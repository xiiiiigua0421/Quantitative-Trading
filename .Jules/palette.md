## 2024-05-24 - Make Scrollable HTML Containers Keyboard-Accessible
**Learning:** Scrollable wrapper containers (`overflow: auto`) without interactive child elements that can receive focus are inaccessible to keyboard users. This prevents users who navigate via keyboard from scrolling the container's content.
**Action:** Added `tabindex="0"`, `role="region"`, and descriptive `aria-label`s to HTML elements with `.chart-wrap` and `.scroll` classes, along with visible `:focus-visible` styles, to ensure keyboard accessibility.
