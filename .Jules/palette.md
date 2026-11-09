## 2026-11-09 - 報表滾動區域無障礙 (Accessible Scrollable Regions)
**Learning:** Scrollable wrapper containers (`overflow: auto` or `scroll`) require explicit focus management and semantic meaning to be keyboard accessible. Users navigating by keyboard cannot scroll content without these attributes, causing them to miss off-screen data.
**Action:** Add `tabindex="0"`, `role="region"`, a descriptive `aria-label`, and a visible `:focus-visible` outline (brand color #16877e) to all scrollable wrappers in HTML reports.
