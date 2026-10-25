## 2024-05-20 - Adding Keyboard Accessibility to Scrollable HTML Reports
**Learning:** Scrollable areas (`overflow: auto` or `scroll`) in generated HTML reports are completely inaccessible to keyboard-only users without explicit tab management.
**Action:** Always add `tabindex="0"`, `role="region"`, a localized `aria-label` (e.g. Traditional Chinese), and a `:focus-visible` outline to scrollable containers.
