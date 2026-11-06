## 2025-02-23 - Make scrollable containers keyboard accessible
**Learning:** Scrollable wrapper containers (`overflow: auto` or `scroll`) in HTML reports are inaccessible to keyboard users unless explicitly made focusable.
**Action:** Always ensure keyboard accessibility by adding `tabindex="0"`, `role="region"`, a descriptive `aria-label` in Traditional Chinese, and a visible `:focus-visible` CSS outline (e.g., using the brand color #16877e) when creating or modifying scrollable wrapper containers.
