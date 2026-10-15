## 2024-10-25 - Scrollable Region Accessibility
**Learning:** Containers with `overflow: auto` (like `.chart-wrap` and `.scroll`) are not keyboard accessible by default. Screen reader and keyboard-only users cannot access the content if they cannot focus on the container to scroll it.
**Action:** Always add `tabindex="0"`, `role="region"`, and an appropriate `aria-label` to scrollable containers, along with a visible `:focus-visible` outline.
