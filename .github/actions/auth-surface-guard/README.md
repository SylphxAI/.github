# `auth-surface-guard`

Enforces the group-wide Single Source of Truth (`SSOT`) Passwordless Auth standard (`SylphxAI/skills` `experience.md`) on pull requests and merge groups:

1. **`AUTH-R1` (`handwritten-auth-route`)**: Rejects new standalone `/signup`, `/register`, `/forgot-password`, or `/reset-password` route files in product web repositories. All web products use a single `Continue` entry (`<SylphxAuth />` from `@sylphx/apps` or the hosted `Sylphx Auth` portal).
2. **`AUTH-R2` (`handwritten-password-input`)**: Rejects new handwritten `<input type="password">` fields in product web UI files (`*.tsx`, `*.jsx`, `*.vue`, `*.svelte`).
3. **`AUTH-R3` (`public-web-sms-otp`)**: Rejects public web SMS OTP sign-in calls (`sendSmsOtp`, `verifySmsOtp`, `signInWithSms`, `signInWithPhone`, `requestSmsOtp`, `smsOtpLogin`) to prevent SMS pumping fraud and split phone/email accounts. Legacy live-title continuity paths may opt out with `# auth-surface-guard: allow(legacy-continuity)` or `// auth-surface-guard: allow(legacy-continuity)`.
