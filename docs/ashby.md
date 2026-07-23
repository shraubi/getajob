# Ashby applications

The bot recognizes public URLs shaped like:

```text
https://jobs.ashbyhq.com/<board>/<job-id>
```

It reads the public Ashby job/form contract, selects the existing local résumé, and preflights every required field before it offers the Telegram confirmation button.

## Applicant profile

`storage/jobs.db` is the runtime source of truth for applicant facts and form
answers. On the first upgraded run, the bot imports the existing untracked
`storage/applicant.json` once and leaves the file untouched:

```json
{
  "first_name": "Ada",
  "last_name": "Lovelace",
  "email": "ada@example.com",
  "phone": "+33123456789",
  "location": {"country": "Exampleland", "city": "Paris"},
  "links": {"linkedin": "https://linkedin.com/in/example"},
  "facts": {
    "work_authorized_countries": ["Exampleland"],
    "previous_employers": ["Example Corp"],
    "application_source_preferences": ["LinkedIn", "Indeed", "Other"]
  },
  "answers": {
    "Exact one-off question title": "Only use this for a vacancy-specific answer"
  }
}
```

Reusable facts are matched semantically, so Ashby UUIDs and small wording changes do not matter. Source preferences are tried in order and only an option actually offered by the form is selected. `previous_employers: []` is an explicit statement that no listed employer applies; omitting the key leaves the answer unresolved.

After migration, new missing answers are requested as one numbered Telegram
batch. A valid reply saves the facts, rechecks the live form, and authorizes
submission for that vacancy. The bot distinguishes authorization, sponsorship
now, and sponsorship in the future, including questions with inverted wording.
Country- and company-specific facts are scoped separately.

The bot never infers work authorization, compensation, demographic data, or
other screening answers. Optional structured screening questions may be
answered with `Skip`. Submitted-answer messages include Forget buttons; these
remove the selected fact for future applications without changing an existing
submission.

## Submission and anti-automation

After explicit Telegram confirmation, Chromium opens the canonical `/application` page, fills the preflighted values, uploads the selected résumé, and presses Ashby's normal submit button. This lets Ashby's invisible reCAPTCHA execute normally.

The bot does not generate, copy, replay, solve, or bypass CAPTCHA tokens. If Ashby displays an interactive challenge, rejects validation, blocks the application, or does not return a verifiable confirmation, the bot:

1. records the attempt and status in SQLite without profile values or résumé contents;
2. keeps the job retryable rather than marking it successful;
3. reports the exact blocker in Telegram;
4. returns the canonical `/application` URL for manual completion.

The production container runs ATS automation in headed Chromium on a private
Xvfb virtual display (`ATS_BROWSER_HEADLESS=false`). This lets Ashby's invisible
reCAPTCHA issue a token as it does in a normal browser; it does not bypass an
interactive challenge. If an interactive challenge is presented, the bot hands
the application back for manual completion. `ASHBY_BROWSER_HEADLESS` remains a
compatibility fallback for existing installations.

The persistent browser profile is stored under `ASHBY_BROWSER_PROFILE_PATH`;
treat it as private runtime state and never commit or copy it casually. A Google
account is not required and credentials must not be placed in `.env` or
`applicant.json`.

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

