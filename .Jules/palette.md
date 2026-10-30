## 2025-02-14 - Accessible Scrollable Regions
**Learning:** Scrollable wrapper containers (`overflow: auto` or `scroll`) used for charts and tables in the HTML reports are completely hidden from screen readers and unreachable by keyboard navigation by default, preventing users from accessing their contents.
**Action:** Always add `tabindex="0"`, `role="region"`, a descriptive `aria-label` (in Traditional Chinese as per app context), and a visible `:focus-visible` CSS outline to `.chart-wrap` and `.scroll` containers.
