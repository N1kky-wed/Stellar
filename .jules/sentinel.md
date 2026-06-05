## 2024-06-05 - Remove Admin Password Override
**Vulnerability:** A hardcoded check for `adminpass` allowed bypassing normal password checks during password updates (`app.py`, `change_user_password`).
**Learning:** Found an administrative backdoor that was potentially exploitable.
**Prevention:** Remove custom environment-based overrides for password verification; rely solely on robust hashing checks.
