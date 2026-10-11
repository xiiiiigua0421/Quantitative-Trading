## 2024-03-28 - Add keyboard navigation to scrollable regions
**Learning:** Scrollable areas with `overflow: auto` or `overflow: scroll` (like data tables and wide SVG charts) are unreachable by keyboard users unless explicitly given `tabindex="0"`. When giving them focus, they also need `role="region"`, a descriptive `aria-label`, and a visible `:focus-visible` outline so users know when the region is focused.
**Action:** Always add `tabindex="0"`, `role="region"`, `aria-label`, and `:focus-visible` styling to containers that have `overflow: auto` or `overflow: scroll` applied to them.
