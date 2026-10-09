## 2024-05-24 - Make Scrollable Areas Keyboard Accessible
**Learning:** Purely visual scrollable areas (like charts or wide data tables with `overflow: auto`) are inaccessible to keyboard users unless explicitly made focusable. Without `tabindex="0"`, screen reader and keyboard-only users cannot scroll into these regions.
**Action:** Always add `tabindex="0"`, an appropriate `role` (like `"region"`), and a descriptive `aria-label` to containers that rely on CSS `overflow` for scrolling content.
