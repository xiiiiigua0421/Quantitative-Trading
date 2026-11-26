## 2024-06-25 - Scrollable regions keyboard accessibility
**Learning:** Tables and charts wrapped in containers with `overflow: auto` are often unreachable via keyboard navigation, preventing screen reader and keyboard-only users from scrolling through their content.
**Action:** Always add `tabindex="0"`, `role="region"`, `aria-labelledby`, and `:focus-visible` styles to scrollable wrapper divs to make them accessible and indicate focus.
