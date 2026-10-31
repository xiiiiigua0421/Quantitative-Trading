## 2024-10-31 - Keyboard Accessibility for Report Scroll Containers
**Learning:** In the generated HTML reports, `overflow: auto` containers (`.chart-wrap` and `.scroll`) are not keyboard-accessible by default, trapping keyboard users from seeing overflowed content.
**Action:** Added `tabindex="0"`, `role="region"`, a descriptive Traditional Chinese `aria-label`, and a clear `:focus-visible` outline to ensure screen reader and keyboard compatibility.
