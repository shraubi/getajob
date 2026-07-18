# Ashby applications

The bot recognizes public URLs shaped like:

```text
https://jobs.ashbyhq.com/<board>/<job-id>
```

It reads the public Ashby job/form contract, selects the existing local résumé, and preflights every required field before it offers the Telegram confirmation button.

## Applicant profile

Add standard values and explicit screening answers to the untracked `storage/applicant.json` file:

```json
{
  "first_name": "Ada",
  "last_name": "Lovelace",
  "email": "ada@example.com",
  "phone": "+33123456789",
  "location": {"country": "France", "city": "Paris"},
  "links": {"linkedin": "https://linkedin.com/in/example"},
  "answers": {
    "ashby-field-path": "An explicit answer",
    "Exact question title": true
  }
}
```

Question paths are preferred because titles can change. The bot never infers work authorization, compensation, demographic data, or other screening answers.

## Submission and anti-automation

After explicit Telegram confirmation, Chromium opens the canonical `/application` page, fills the preflighted values, uploads the selected résumé, and presses Ashby's normal submit button. This lets Ashby's invisible reCAPTCHA execute normally.

The bot does not generate, copy, replay, solve, or bypass CAPTCHA tokens. If Ashby displays an interactive challenge, rejects validation, blocks the application, or does not return a verifiable confirmation, the bot:

1. records the attempt and status in SQLite without profile values or résumé contents;
2. keeps the job retryable rather than marking it successful;
3. reports the exact blocker in Telegram;
4. returns the canonical `/application` URL for manual completion.

Set `ASHBY_BROWSER_HEADLESS=false` only where an operator can access the browser display. The persistent browser profile is stored under `ASHBY_BROWSER_PROFILE_PATH`; treat it as private runtime state and never commit or copy it casually. A Google account is not required and credentials must not be placed in `.env` or `applicant.json`.

## Limits

- Public vacancy/form reads need no credentials.
- Browser submission depends on Ashby's current hosted form and may require manual intervention.
- The integration does not submit optional demographic surveys.
- It does not bypass authentication, CAPTCHA, application limits, rate limits, or other anti-automation controls.
- Success requires visible confirmation text; an HTTP 200 or redirect alone is not sufficient.
- Résumés over Ashby's documented 50 MB form limit are rejected before browser launch.

## Live smoke test

Only use a controlled/test vacancy:

```bash
ASHBY_LIVE_SMOKE_URL=https://jobs.ashbyhq.com/<board>/<job-id> \
python -m unittest tests.test_ashby.AshbyLiveSmokeTests -v
```

The smoke test reads the public contract only and never submits.

## Rollback

Revert the Ashby commits and rebuild the container. Keep `storage/jobs.db`, `storage/applicant.json`, and the browser profile as private runtime data. Removing `storage/ashby-browser` signs out and resets the browser profile but is not required for code rollback.
