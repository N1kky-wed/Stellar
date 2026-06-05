## 2024-06-05 - Icon-only buttons lack ARIA labels
**Learning:** Found an accessibility issue pattern across this app's UI components where icon-only buttons rely exclusively on 'title' attributes or visual icons without explicit ARIA labels for screen readers.
**Action:** Added 'aria-label' attributes to multiple icon-only buttons (like toggle sidebar, close panels, send message) across index.html to ensure screen reader compatibility.
