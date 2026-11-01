## 2024-05-24 - Add keyboard accessibility to scrollable reports
**Learning:** Scrollable wrapper containers (`overflow: auto` or `scroll`) in HTML reports are inaccessible to keyboard users unless explicitly made focusable.
**Action:** Always add `tabindex="0"`, `role="region"`, a descriptive `aria-label` in Traditional Chinese, and a visible `:focus-visible` CSS outline to scrollable containers.
