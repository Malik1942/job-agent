# jobagent

A local, personal job-application pipeline: scan public ATS job boards, score
openings against your profile, generate tailored cover letters, and
(optionally) submit applications by filling forms from a reference answers
file. An autonomy dial and a master safety switch keep you in control, and a
browser-based setup wizard personalizes everything — no personal data ships
with this repo.

**The safety model, up front:**

- Everything runs and stays on your machine. Your profile, answers, resumes,
  database, and logs are git-ignored and never leave except inside the
  applications you choose to send.
- `autonomy.mode: review` (the default) holds every eligible job for your
  approval and doesn't even open a browser. `autonomy.live_submit: false`
  (also the default) is the master switch — until you flip it, "auto" mode
  only fills and screenshots, never submits.
- The filler never guesses: a required field it can't answer from YOUR facts
  holds the job for you instead of submitting something half-filled.
- ATS platforms whose anti-fraud systems flag automated submits (Ashby) are
  prepared-but-never-submitted: the agent fills everything up to the Submit
  button and you click it yourself. Compliance, not evasion.

## Quick start

```bash
git clone <this-repo> jobagent && cd jobagent
python3.12 -m venv .venv && source .venv/bin/activate

pip install -e ".[browser]"     # or ".[all]" to add LLM + Slack extras
playwright install chromium     # one-time, needed for auto-fill

jobagent setup --config config.yaml   # opens the setup wizard in your browser
jobagent scan  --config config.yaml   # discover + rank; read-only, submits nothing
```

Requires Python 3.10+. On macOS `python3` may still be 3.9, so use
`python3.12` when creating the venv if available.

### What the wizard writes

`jobagent setup` walks you through 7 steps (basics, work authorization,
targeting, resumes, cover-letter voices, facts, review) and writes two files:

- **`config.yaml`** — profile, targeting, sources, resume variants, and
  cover-letter content. Generated with safe defaults (`review` mode, live
  submit off). Every advanced option is documented in `config.example.yaml`;
  edit the file directly or re-run the wizard any time (it pre-loads your
  current values, and backs up files before overwriting).
- **`answers.md`** — the facts file the form-filler answers from. Format
  details in `answers.example.md`.

What it deliberately does **not** touch: secrets. Slack tokens, email app
passwords, and API keys live in `scripts/.env` (copy `scripts/.env.example`)
or your shell environment — never in config files, never in the wizard.

## What it does, honestly

Auto-apply runs against sources with documented public job-board APIs. For
those it can discover, rank, write a cover letter, fill the application form,
and (with both safety switches flipped) submit.

| kind              | careers URL pattern                  | auto-apply | notes |
|-------------------|--------------------------------------|------------|-------|
| `greenhouse`      | boards.greenhouse.io/{token}         | yes        | supports emailed verification codes (see below) |
| `lever`           | jobs.lever.co/{token}                | yes        | |
| `ashby`           | jobs.ashbyhq.com/{token}             | fill-only  | Ashby flags automated submits, so the agent prepares everything and YOU click submit |
| `workable`        | apply.workable.com/{token}           | yes        | |
| `smartrecruiters` | careers.smartrecruiters.com/{token}  | yes        | fetches per-posting details for descriptions |
| `recruitee`       | {token}.recruitee.com                | yes        | |
| `adzuna`          | (aggregator search)                  | discovery  | needs `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` env vars; results are scan-only unless the apply URL is on an ATS above |
| `scan_only`       | —                                    | never      | surface-and-rank placeholder for boards you handle by hand |

**LinkedIn, Indeed, ZipRecruiter, and Workday are scan-and-rank only — never
automated.** Scripting submissions to them violates their terms of service and
risks your account, so this tool will not do it, and Adzuna results that
redirect to them stay scan-only.

Things it will never do:

- **Solve a CAPTCHA.** The filler stops the moment it sees one and hands the
  job back to you.
- **Guess on an application.** If a REQUIRED field cannot be answered from your
  answers file (and the optional LLM cannot ground an answer in your facts),
  the job is held for you instead of submitted half-filled.
- **Submit from a datacenter IP.** `autonomy.block_datacenter_ip` (default on)
  refuses to click submit when your egress IP looks like a VPN/datacenter —
  the #1 ATS fraud signal. Turn the VPN off and rerun.
- **Auto-submit manual-only companies.** Add dream companies to
  `autonomy.manual_only_companies` to keep them ranked but never auto-filled.

## Cover letters (your voice, not ours)

The letter generator supplies structure; **you supply the voice** in the
`coverletter:` section of config.yaml (the wizard's step 5 writes it):

- **Families** — one framing per role type (e.g. `product_designer`,
  `design_engineer`), matched against the job title by keywords. Each has an
  identity paragraph, a craft paragraph, a closing mix, and a `marker` phrase
  the validator requires in the finished letter — so a letter framed for the
  wrong role family can never ship.
- **Projects** — short project stories included when their keywords appear in
  the job text.
- Your `profile.website` must appear in every letter (validated) when set.

Letters are deterministic by default. With an LLM configured and
`llm.cover_letters: true`, the LLM composes them from the same config facts
and the same validation gates apply.

## The answers file (how auto-submit fills forms)

Auto-submit reads answers from a markdown file (`profile.answers_path`, see
`answers.example.md`). Three sections:

- `## Fields`    short reusable values (name, email, work authorization, links)
- `## Questions` your prepared answers to common free-text prompts (Q:/A:)
- `## Context`   free-form background the LLM may draw on, only if you enable it

For each form field, the filler matches the field's label to the closest thing
in your file: known field aliases, then a fuzzy match to your Q/A, then (if
`apply.use_llm_for_answers: true`) a constrained LLM answer that returns
nothing rather than inventing anything. Preview offline:

```bash
jobagent answers  --config config.yaml   # how sample fields would be answered
jobagent coverage --config config.yaml   # which expected ATS fields have no answer yet
jobagent holds    --config config.yaml   # which required fields blocked past fills
```

Each `holds` line is one answers.md addition away from unlocking every
application it held.

## Commands

```bash
jobagent setup   --config config.yaml   # first-run setup wizard (browser)
jobagent scan    --config config.yaml   # discover + rank, no side effects
jobagent answers --config config.yaml   # preview form answers, offline
jobagent coverage --config config.yaml  # answers-file coverage per ATS
jobagent holds   --config config.yaml   # required fields that block submissions
jobagent run     --config config.yaml   # match + generate + apply + track
jobagent status  --config config.yaml   # show the tracker
jobagent review-queue --config config.yaml --interactive  # Slack review queue
jobagent slack-panel --config config.yaml    # Slack button control panel
jobagent approval-bot --config config.yaml   # listen for Slack buttons
jobagent approval-web --config config.yaml   # phone web approval panel
jobagent approval-web-link --config config.yaml  # send panel link to Slack
jobagent ashby-checklist --config config.yaml    # crib sheet for manual Ashby submits
jobagent probe-ashby --config config.yaml --url <posting>  # attended Ashby diagnostic
jobagent test-notify --config config.yaml    # verify a notification channel
jobagent test-llm --config config.yaml       # verify Claude/OpenAI credentials

# Equivalent without the console script:
python -m jobagent.cli scan --config config.yaml
```

`scan`, `run`, and `review-queue` refuse to start until your profile is
complete — they print exactly what's missing and point you to
`jobagent setup`.

## LLM setup (optional, fail-soft)

If no provider is configured, the agent uses template cover letters and holds
any unknown required form fields.

```yaml
apply:
  use_llm_for_answers: true
llm:
  provider: "auto"       # auto | anthropic | openai | off
  cover_letters: false   # true lets the daily run use LLM cover letters
```

Then put one provider in `scripts/.env` or your shell:

```bash
export ANTHROPIC_API_KEY='...'   # Claude
export OPENAI_API_KEY='...'      # OpenAI / Codex-compatible
```

Run `jobagent test-llm --config config.yaml` before relying on LLM answers.

## Emailed verification codes (optional)

Some boards (e.g. Greenhouse-hosted ones) email a security code after submit
that must be entered to complete the application. With `email_verify.enabled:
true` and an app password for the SAME inbox your applications use, the filler
reads the code over IMAP and finishes the step — for the real applicant, which
is what the check is for. Three trust gates verify sender, format, and timing
before any code is used (the inbox is untrusted input); an ATS whose code
email format hasn't been calibrated HOLDS the job rather than guessing.

## Ranking preferences

The `scoring` block boosts new-grad / entry-level roles and gently penalizes
senior/staff/principal titles — tune `preferred_bonus` and `penalty`, or edit
`role_priority` for your field. `filters.title_exclude_keywords` drops
hardware/facilities "Design Engineer" false positives; adjust for your domain.

## Slack review queue

Use `jobagent review-queue --config config.yaml` while in review mode. It
sends one Slack message per auto-eligible job with company, role, score,
match notes, and link, and saves the queue to `output/review_queues/`.

For phone approvals inside Slack, upgrade the Slack app to Socket Mode:

1. In your Slack app, add bot scope `chat:write` under **OAuth & Permissions**,
   reinstall the app, and copy the bot token (`xoxb-...`).
2. Under **Socket Mode**, enable it and create an app-level token with
   `connections:write` (`xapp-...`).
3. Under **Interactivity & Shortcuts**, enable interactivity (no public URL
   needed with Socket Mode).
4. Put the tokens in `scripts/.env` as `JOBAGENT_SLACK_BOT_TOKEN` and
   `JOBAGENT_SLACK_APP_TOKEN`; set `JOBAGENT_SLACK_CHANNEL` to your channel ID.
5. Run:

```bash
jobagent approval-bot --config config.yaml --post-panel
jobagent review-queue --config config.yaml --interactive
```

Leave `approval-bot` running on the computer that should do the browser work.
The Slack control panel gives you **Refresh / Queue Status / Approve All /
Stop Batch / Skip All**, and each job card has **Approve** / **Skip**.

### Two-phase approvals (default)

With `autonomy.two_phase_approval: true`, nothing submits on the first tap:

1. **Approve** fills the form, screenshots it, and posts the screenshot plus
   the field list to Slack. Nothing is submitted.
2. **Confirm submit** re-runs the fill and actually submits.

**Approve All** fills everything then offers one **Confirm all** button. Batch
submissions are spaced by a random pause (`autonomy.batch_pause_*`) and each
is preceded by a liveness check that skips postings which closed since the
scan.

### Slash commands

```
/jobagent refresh [N]      scan fresh, queue + post top N
/jobagent status           queue status counts
/jobagent approve JA-001|all   fill + screenshot (no submit)
/jobagent confirm JA-001|all   submit reviewed item(s)
/jobagent stop             stop the current batch
/jobagent skip JA-001|all  skip pending item(s)
/jobagent holds            blocking required fields
/jobagent coverage         answers coverage per ATS
/jobagent checklist        Ashby manual-submit crib sheet
```

One-time setup: api.slack.com/apps → your app → **Slash Commands** → create
`/jobagent`. With Socket Mode no Request URL is needed.

### Phone web approval fallback

If Slack buttons misbehave, the local phone panel serves real Approve/Confirm
buttons from your machine:

```bash
jobagent approval-web --config config.yaml        # binds 127.0.0.1 by default
jobagent approval-web-link --config config.yaml   # send the tokened link to Slack
```

Reach it from your phone by setting `approval_web.host` to your Tailscale/VPN
IP (recommended). `0.0.0.0` exposes it to the whole network — it warns you.
URLs carry a random per-start token.

## Turning on auto-submit (do this deliberately)

1. Complete `jobagent setup`; run `answers` and `coverage` until clean.
2. Drop your resume PDF(s) in place.
3. Set `autonomy.mode: auto`, keep `live_submit: false`, keep
   `apply.headless: false`. Watch it fill and screenshot without submitting.
4. Review screenshots in `output/screenshots/`. When you trust it, set
   `autonomy.live_submit: true`.

Start slow. Unreviewed mass-applying tanks response rates — recruiters spot
generic auto-apply fast. The filler screenshots everything and holds anything
it is unsure about.

## Submitted-jobs CSV log

Every submitted job is appended to `csv_path` (default `output/applied.csv`),
UTF-8 with BOM, deduped by job link. Only statuses in `csv_statuses` are
logged (default `applied`).

## Notifications (new matches)

After each run, jobagent can email you or post to Slack about genuinely new
records. Configure the `notify` block; secrets come from env vars only:

```bash
export JOBAGENT_SMTP_PASSWORD='your-email-app-password'
export JOBAGENT_SLACK_WEBHOOK='https://hooks.slack.com/services/...'
jobagent test-notify --config config.yaml
```

## Daily scheduling

macOS (launchd) — templates render to YOUR checkout path automatically:

```bash
cp scripts/.env.example scripts/.env   # fill in secrets first
scripts/install_launchd.sh --dry-run   # see what would be installed
scripts/install_launchd.sh             # render + load all agents
```

This installs the daily run, the Slack approval bot, the phone approval
panel, and a bot health-check (restarts the bot if its Slack socket wedges).

Linux (cron), daily at 09:00:

```
0 9 * * * JOBAGENT_DIR=$HOME/jobagent /bin/bash $HOME/jobagent/scripts/run_daily.sh
```

## Privacy

- `config.yaml`, `answers.md`, `jobagent.db`, `output/`, resume PDFs, and
  `scripts/.env` are git-ignored: your personal data never enters version
  control.
- `output/` accumulates screenshots, cover letters, and logs containing your
  personal data — treat it accordingly and never publish it.
- The example persona ("Alex Rivera") is fictional. The readiness gate stops
  you from accidentally running with example values.

## Layout

```
jobagent/
  config.py         typed config loader (+ coverletter families, profile gaps)
  models.py         Job + ApplicationRecord, with a stable uid for dedupe
  scoring.py        transparent, explainable scoring (every score has reasons)
  coverletter.py    letter structure; all voice/content comes from config
  answers.py        parse answers.md and resolve form questions to answers
  browser.py        Playwright form filler: fill, screenshot, CAPTCHA backoff
  apply.py          the autonomy dial and live_submit switch live here
  setup_wizard.py   `jobagent setup`: localhost wizard -> config.yaml + answers.md
  email_verify.py   IMAP verification-code completion (three trust gates)
  netcheck.py       datacenter-IP preflight (compliance gate)
  gh_questions.py   per-job Greenhouse question prefetch
  ashby_form.py     Ashby field mapping + manual-submit checklist
  probe.py          attended Ashby diagnostic (never auto-submits)
  coverage.py       answers-file coverage report per ATS
  notify.py         email + Slack notifications for new matches
  tracker.py        SQLite dedupe + status log + CSV log
  pipeline.py       scan -> score -> generate -> apply -> track -> notify
  review_queue.py   Slack metadata queue for human confirmation
  slack_approval.py Socket Mode approval bot + slash commands
  web_approval.py   phone approval panel (localhost, tokened)
  verification_data.py  calibrated anchors for verification codes
  cli.py            command dispatch
  sources/          greenhouse, lever, ashby, workable, smartrecruiters,
                    recruitee, adzuna connectors + registry
scripts/
  install_launchd.sh        render + install the launchd templates (macOS)
  templates/*.plist.template  launchd agents (daily, bot, panel, healthcheck)
  run_daily.sh              cron/launchd wrapper
  bot_healthcheck.sh        restart the approval bot if its socket wedges
  export_public.sh          maintainers: build the public release
  check_no_personal.sh      maintainers: personal-data leak gate
  .env.example              secret template (copy to scripts/.env)
  test_*.py                 offline test scripts (plain asserts, no framework)
```
