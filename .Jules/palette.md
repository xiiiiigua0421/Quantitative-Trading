## 2024-05-18 - Keyboard Accessibility for Scrollable Elements
**Learning:** Scrollable wrapper containers (`overflow: auto` or `scroll`) in HTML reports can trap keyboard users if they cannot focus them to scroll. Using custom styled `div` instead of native semantic elements can hinder accessibility.
**Action:** When creating or modifying scrollable wrapper containers, always ensure keyboard accessibility by adding `tabindex="0"`, `role="region"`, a descriptive `aria-label` in Traditional Chinese, and a visible `:focus-visible` CSS outline.
