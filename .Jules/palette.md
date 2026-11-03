## 2026-11-03 - Accessible Scrollable Regions
**Learning:** In reports with large tables or charts, adding `overflow: auto` limits screen real estate use but prevents keyboard users from easily scrolling the content when they navigate by tab.
**Action:** When creating scrollable wrappers (`.chart-wrap`, `.scroll`), always add `tabindex="0"`, `role="region"`, a descriptive `aria-label`, and a clear `:focus-visible` outline for keyboard navigation.
