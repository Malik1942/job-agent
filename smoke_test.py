"""End-to-end logic check with mock jobs and the answer matcher.

Default mode does not use network or a browser. Add --browser to exercise the
Playwright filler against a local HTML form.
"""

import os
from pathlib import Path
import sys
import tempfile

if sys.version_info < (3, 10):
    raise SystemExit(
        "jobagent requires Python 3.10+. Run this with .venv/bin/python "
        "or create the venv with python3.12."
    )

try:
    from jobagent.config import load_config
    from jobagent.models import Job
    from jobagent.scoring import score_all
    from jobagent.coverletter import (
        cover_letter_pdf_path,
        validate_cover_letter_package,
        write_cover_letter,
    )
    from jobagent.apply import decide
    from jobagent.tracker import Tracker
    from jobagent.answers import AnswerBook
    from jobagent.config import Source
    from jobagent.review_queue import _control_panel_blocks, build_review_items
    from jobagent.models import ApplicationRecord
except ModuleNotFoundError as exc:
    raise SystemExit(
        f"Missing dependency: {exc.name}. Activate .venv or run "
        "python -m pip install -e '.[all]'."
    ) from exc

RUN_BROWSER = "--browser" in sys.argv

cfg = load_config("config.example.yaml")
smoke_dir = tempfile.mkdtemp(prefix="jobagent-smoke-")
cfg.db_path = os.path.join(smoke_dir, "jobagent_smoke.db")
cfg.output_dir = os.path.join(smoke_dir, "output")
if os.path.exists(cfg.db_path):
    os.remove(cfg.db_path)

mock = [
    Job(source="greenhouse", company="Figma", title="New Grad Product Designer",
        url="https://ex/0", location="Remote",
        description="Entry-level role for recent graduates using Figma, "
                    "prototyping, design systems, user research, and AI"),
    Job(source="greenhouse", company="Linear", title="Senior UX Designer",
        url="https://ex/1", location="Remote",
        description="Figma, design systems, prototyping, user research, UX"),
    Job(source="lever", company="Netflix", title="Product Designer",
        url="https://ex/2", location="Seattle, WA",
        description="React and prototyping for AI products"),
    Job(source="ashby", company="SomeCo", title="Unpaid UX Design Intern",
        url="https://ex/3", location="Remote", description="unpaid internship"),
    Job(source="linkedin", company="BigCo", title="UX Researcher",
        url="https://ex/4", location="Seattle", scan_only=True,
        description="user research, AI"),
    Job(source="greenhouse", company="FarCo", title="Backend Engineer",
        url="https://ex/5", location="Austin, TX",
        description="Go, Kubernetes, distributed systems"),
]

scored = score_all(mock, cfg)
scores_by_title = {j.title: j.score for j in scored}
assert (
    scores_by_title["New Grad Product Designer"]
    > scores_by_title["Senior UX Designer"]
)
print("=== SCORING (sorted) ===")
for j in scored:
    tag = " [scan-only]" if j.scan_only else ""
    print(f"{j.score:>4.2f}  {j.company} - {j.title}{tag}  :: {'; '.join(j.reasons)}")

print("\n=== MANUAL-ONLY COMPANIES ===")
import jobagent.pipeline as pipeline_mod

real_fetch_source = pipeline_mod.fetch_source
cfg.sources = [Source(kind="greenhouse", token="dream", label="Dream")]
pipeline_mod.fetch_source = lambda src: ([
    Job(source="greenhouse", company="Google DeepMind",
        title="AI Product Designer", url="https://ex/google",
        location="Remote", description="AI product design and prototyping"),
    Job(source="greenhouse", company="SmallCo",
        title="AI Product Designer", url="https://ex/smallco",
        location="Remote", description="AI product design and prototyping"),
], "")
try:
    scan_result = pipeline_mod.scan(cfg)
finally:
    pipeline_mod.fetch_source = real_fetch_source
manual = {j.company: j for j in scan_result.jobs}
assert manual["Google DeepMind"].scan_only
assert not manual["SmallCo"].scan_only
assert "manual-only company" in " ".join(manual["Google DeepMind"].reasons)
print("  Google DeepMind -> scan_only")
print("  SmallCo         -> auto-eligible")

review_items = build_review_items(cfg, scan_result.jobs)
assert [i.id for i in review_items] == ["JA-001"]
assert review_items[0].company == "SmallCo"
print("  review queue    -> SmallCo only")

panel_blocks = _control_panel_blocks(cfg)
panel_actions = {
    e.get("action_id")
    for b in panel_blocks if b.get("type") == "actions"
    for e in b.get("elements", [])
}
assert {
    "jobagent_queue_top_5", "jobagent_queue_top_25", "jobagent_queue_status",
    "jobagent_approve_all", "jobagent_stop_batch", "jobagent_skip_all",
    "jobagent_coverage", "jobagent_scan", "jobagent_ashby_checklist",
}.issubset(panel_actions)
print("  slack panel     -> button actions present (incl. report buttons)")

import json
import jobagent.slack_approval as approval_mod

latest_dir = Path(cfg.output_dir) / "review_queues"
latest_dir.mkdir(parents=True, exist_ok=True)
latest_real = latest_dir / "job-review-queue-20260702-000000.json"
latest_stop = latest_dir / "job-review-queue-20260702-000000.stop.json"
latest_real.write_text(json.dumps({"queue": []}), encoding="utf-8")
latest_stop.write_text(json.dumps({"reason": "test"}), encoding="utf-8")
assert approval_mod.latest_queue_path(cfg) == latest_real
print("  latest queue    -> ignores stop marker files")

queue_path = Path(smoke_dir) / "review-queue.json"
queue_path.write_text(json.dumps({"queue": [
    {"id": "JA-001", "company": "SmallCo", "title": "AI Product Designer",
     "score": 0.93, "location": "Remote", "source": "greenhouse",
     "url": "https://ex/smallco", "apply_url": "https://ex/smallco/apply",
     "reasons": [], "status": "pending_confirmation"},
    {"id": "JA-002", "company": "OtherCo", "title": "Design Engineer",
     "score": 0.80, "location": "Remote", "source": "ashby",
     "url": "https://ex/other", "apply_url": "https://ex/other/apply",
     "reasons": [], "status": "pending_confirmation"},
    {"id": "JA-003", "company": "SkipCo", "title": "UX Designer",
     "score": 0.70, "location": "Remote", "source": "lever",
     "url": "https://ex/skip", "apply_url": "https://ex/skip/apply",
     "reasons": [], "status": "skipped_by_user"},
]}), encoding="utf-8")

real_decide = approval_mod.decide
real_db_path = cfg.db_path
real_csv_path = cfg.csv_path
real_liveness = approval_mod.check_posting_live
real_pause = (cfg.autonomy.batch_pause_min_seconds,
              cfg.autonomy.batch_pause_max_seconds)
cfg.db_path = str(Path(smoke_dir) / "approval_smoke.db")
cfg.csv_path = ""
cfg.autonomy.batch_pause_min_seconds = 0
cfg.autonomy.batch_pause_max_seconds = 0
approval_mod.check_posting_live = lambda url, timeout=15: (True, "mock live")
approval_mod.decide = lambda job, cfg, cover, book=None: ApplicationRecord(
    uid=job.uid, company=job.company, title=job.title, url=job.url,
    score=job.score, status="needs_review", note="mock approved",
    cover_letter_path=cover, location=job.location, source=job.source,
    screenshot="output/screenshots/mock.png",
    filled_fields=["First Name = Alex [field]", "Gender = Male [field]"],
)
try:
    approved, errors = approval_mod.approve_all_queue_items(cfg, queue_path)
finally:
    approval_mod.decide = real_decide
    cfg.db_path = real_db_path
    cfg.csv_path = real_csv_path
    approval_mod.check_posting_live = real_liveness
    (cfg.autonomy.batch_pause_min_seconds,
     cfg.autonomy.batch_pause_max_seconds) = real_pause
updated_queue = json.loads(queue_path.read_text(encoding="utf-8"))["queue"]
assert len(approved) == 2 and not errors
assert [i["status"] for i in updated_queue] == [
    "needs_review", "needs_review", "skipped_by_user",
]
assert updated_queue[0]["filled_fields"] == [
    "First Name = Alex [field]", "Gender = Male [field]",
]
report = approval_mod.submission_report_text(
    queue_path, ["JA-001", "JA-002"], "Approve All", errors,
)
assert "jobagent submission report" in report
assert "Filled fields (2)" in report
assert "Gender = Male" in report
print("  approve all     -> pending only")

stop_queue_path = Path(smoke_dir) / "review-queue-stop.json"
stop_queue_path.write_text(json.dumps({"queue": [
    {"id": "JA-011", "company": "OneCo", "title": "AI Product Designer",
     "score": 0.93, "location": "Remote", "source": "greenhouse",
     "url": "https://ex/one", "apply_url": "https://ex/one/apply",
     "reasons": [], "status": "pending_confirmation"},
    {"id": "JA-012", "company": "TwoCo", "title": "Design Engineer",
     "score": 0.80, "location": "Remote", "source": "ashby",
     "url": "https://ex/two", "apply_url": "https://ex/two/apply",
     "reasons": [], "status": "pending_confirmation"},
    {"id": "JA-013", "company": "ThreeCo", "title": "UX Designer",
     "score": 0.70, "location": "Remote", "source": "lever",
     "url": "https://ex/three", "apply_url": "https://ex/three/apply",
     "reasons": [], "status": "pending_confirmation"},
]}), encoding="utf-8")
def _stop_after_first(job, cfg, cover, book=None):
    if job.company == "OneCo":
        approval_mod.request_batch_stop(stop_queue_path, "test")
    return ApplicationRecord(
        uid=job.uid, company=job.company, title=job.title, url=job.url,
        score=job.score, status="needs_review", note="mock approved",
        cover_letter_path=cover, location=job.location, source=job.source,
    )


approval_mod.decide = _stop_after_first
cfg.db_path = str(Path(smoke_dir) / "approval_stop_smoke.db")
cfg.csv_path = ""
approval_mod.check_posting_live = lambda url, timeout=15: (True, "mock live")
try:
    stopped_records, stop_errors = approval_mod.approve_all_queue_items(
        cfg, stop_queue_path,
    )
finally:
    approval_mod.decide = real_decide
    cfg.db_path = real_db_path
    cfg.csv_path = real_csv_path
    approval_mod.check_posting_live = real_liveness
stop_q = json.loads(stop_queue_path.read_text(encoding="utf-8"))["queue"]
assert len(stopped_records) == 1
assert any("batch stopped by user" in e for e in stop_errors)
assert [i["status"] for i in stop_q] == [
    "needs_review", "pending_confirmation", "pending_confirmation",
]
assert approval_mod.batch_stop_requested(stop_queue_path)
approval_mod.clear_batch_stop(stop_queue_path)
print("  stop batch      -> cancels after current item; leaves rest pending")

queue_path.write_text(json.dumps({"queue": [
    {"id": "JA-001", "status": "pending_confirmation"},
    {"id": "JA-002", "status": "needs_review"},
    {"id": "JA-003", "status": "pending_confirmation"},
]}), encoding="utf-8")
skipped = approval_mod.skip_all_queue_items(queue_path)
skip_queue = json.loads(queue_path.read_text(encoding="utf-8"))["queue"]
assert skipped == 2
assert [i["status"] for i in skip_queue] == [
    "skipped_by_user", "needs_review", "skipped_by_user",
]
print("  skip all        -> pending only")

print("\n=== ANSWER MATCHER (answers.example.md vs typical ATS fields) ===")
book = AnswerBook.from_file("answers.example.md")
tests = [
    ("First Name", "text", None),
    ("Email Address", "text", None),
    ("Are you legally authorized to work in the US?", "select", ["Yes", "No"]),
    ("Will you now or in the future require visa sponsorship?", "select", ["Yes", "No"]),
    ("LinkedIn Profile", "text", None),
    ("Pronunciation guide", "text", None),
    ("Why do you want to work here?", "textarea", None),
    ("Years of experience with Kubernetes", "text", None),  # not covered -> HOLD
]
for label, kind, opts in tests:
    r = book.resolve(label, kind, opts)
    if r is None:
        print(f"  [HOLD]     {label}")
    else:
        v = r.value if len(r.value) <= 70 else r.value[:67] + "..."
        print(f"  [{r.source:^8}] {label} -> {v}")

cover_check = write_cover_letter(scored[0], cfg)
cover_pdf_check = cover_letter_pdf_path(scored[0], cfg)
cover_check_text = Path(cover_check).read_text(encoding="utf-8")
assert Path(cover_pdf_check).exists()
# The letter's identity paragraph comes from the config's matched family.
from jobagent.coverletter import role_family as _role_family
_fam = cfg.coverletter.families[_role_family(scored[0].title, cfg)]
assert _fam.identity in cover_check_text
assert _fam.marker in cover_check_text.lower()
assert scored[0].company in cover_check_text
assert scored[0].title in cover_check_text
assert Path(cover_pdf_check).name.startswith("Alex_Rivera_Cover_Letter_")
assert not validate_cover_letter_package(
    scored[0], cfg, cover_check, cover_pdf_check
)
bad_cover = Path(smoke_dir) / "wrong-cover.txt"
bad_cover.write_text(
    "Dear OtherCo Hiring Team,\n\n"
    "I am excited to apply for the Backend Engineer role.\n\n"
    "Warmly,\nAlex Rivera\n",
    encoding="utf-8",
)
bad_errors = validate_cover_letter_package(scored[0], cfg, str(bad_cover))
assert any("company" in err for err in bad_errors)
assert any("role" in err for err in bad_errors)
print("  cover package -> role-specific text + PDF; wrong letter blocked")

logistics_book = AnswerBook.from_text("""## Fields
- location: Seattle, WA
- start_date: TODO_CONFIRM
- office_three_days_per_week: TODO_CONFIRM
""")
assert logistics_book.resolve("Where are you currently located?", "text").value == "Seattle, WA"
assert logistics_book.resolve("When can you start a new role?", "text") is None
assert logistics_book.resolve(
    "Are you able to work from our US office three days per week?",
    "select",
    ["Yes", "No"],
) is None
print("  logistics -> current location fills; TODO start/office fields hold")

office_book = AnswerBook.from_text("""## Fields
- office_three_days_per_week: "Yes"
""")
assert office_book.resolve(
    "Are you able to work from our US office three days per week?",
    "select",
    ["Yes", "No"],
).value == "Yes"

work_book = AnswerBook.from_text("""## Fields
- work_1_company: Northwind Studio
- work_1_title: Technology and Equipment Manager
- work_1_location: Seattle, WA
- work_1_start_month: September
- work_1_start_year: 2025
- work_1_start_date: September 2025
- work_1_end_month: Present
- work_1_end_year: Present
- work_1_end_date: Present
- work_1_current: "Yes"
- work_1_description: Designed an online inventory system to streamline equipment access.
- work_2_company: Brightline Labs
- work_2_title: UX Designer Intern
- work_2_location: Remote
- work_2_start_month: March
- work_2_start_year: 2025
- work_2_start_date: March 2025
- work_2_end_month: August
- work_2_end_year: 2025
- work_2_end_date: August 2025
- work_2_current: "No"
- work_2_description: Worked on UX design for retail and service experiences.
""")
assert work_book.work_experience_count() == 2
work_meta = "Work Experience Employment History"
work_tests = [
    ("Company", "text", None, 1, "Northwind Studio"),
    ("Job Title", "text", None, 1, "Technology and Equipment Manager"),
    ("Location", "text", None, 2, "Remote"),
    ("Start Month", "select", ["March", "September"], 2, "March"),
    ("End Year", "select", ["2024", "2025", "Present"], 1, "Present"),
    ("Description", "textarea", None, 2,
     "Worked on UX design for retail and service experiences."),
]
for label, kind, opts, idx, expected in work_tests:
    r = work_book.resolve_work_experience(
        label, kind, opts, index=idx, meta=work_meta,
    )
    assert r and r.value == expected, (label, idx, r.value if r else None)
assert work_book.resolve_work_experience("Location", "text", None, 1) is None
print("  work experience -> structured resume data")

eeo_book = AnswerBook.from_text("""## Fields
- gender: Male
- hispanic_latino: "No"
- race: Asian
- veteran_status: I am not a protected veteran
- disability_status: No, I do not have a disability and have not had one in the past
""")
eeo_tests = [
    ("Gender", ["Male", "Female", "Decline to self-identify"], "Male"),
    ("Gender", ["Female", "Male", "Decline to self-identify"], "Male"),
    ("Gender Identity", ["Female", "Non-binary", "Male"], "Male"),
    ("Gender Identity", ["Woman", "I identify as a man", "Non-binary"],
     "I identify as a man"),
    ("Are you Hispanic/Latino?", ["Yes", "No", "I don't wish to answer"], "No"),
    ("Are you Hispanic/Latino?", [
        "I don't wish to answer", "Hispanic or Latino", "Not Hispanic or Latino",
    ], "Not Hispanic or Latino"),
    ("Race", ["Asian", "White", "Decline to self-identify"], "Asian"),
    ("Race / Ethnicity", [
        "White", "Black or African American", "Asian (Not Hispanic or Latino)",
        "Decline to self-identify",
    ], "Asian (Not Hispanic or Latino)"),
    ("Veteran Status", [
        "I am not a protected veteran",
        "I identify as one or more of the classifications of protected veteran",
        "I don't wish to answer",
    ], "I am not a protected veteran"),
    ("Veteran Status", [
        "I don't wish to answer",
        "I identify as one or more of the classifications of protected veteran",
        "Not a protected veteran",
    ], "Not a protected veteran"),
    ("Veteran Status", [
        "I don't wish to answer",
        "Protected veteran",
        "Not a protected veteran",
    ], "Not a protected veteran"),
    ("Disability Status", [
        "Yes, I have a disability, or have had one in the past",
        "No, I do not have a disability and have not had one in the past",
        "I don't wish to answer",
    ], "No, I do not have a disability and have not had one in the past"),
    ("Disability Status", [
        "I don't wish to answer",
        "Yes, I have a disability, or have had one in the past",
        "I do not have a disability",
    ], "I do not have a disability"),
]
for label, opts, expected in eeo_tests:
    r = eeo_book.resolve(label, "select", opts)
    assert r and r.value == expected, (label, r.value if r else None)
print("  EEO fields -> gender/male, non-Hispanic, Asian, not veteran, no disability")

print("\n=== APPLY DECISIONS (mode=review, live_submit=false) ===")
tracker = Tracker(cfg.db_path)
for j in scored:
    cover = ""
    if j.score >= cfg.autonomy.min_score and not j.scan_only:
        cover = write_cover_letter(j, cfg)
    rec = decide(j, cfg, cover, book=book)
    tracker.record(rec)
    print(f"[{rec.status}] {rec.company} - {rec.title} :: {rec.note}")

print("\n=== DEDUPE CHECK (re-run same jobs) ===")
new = sum(0 if tracker.seen(j.uid) else 1 for j in scored)
print(f"already seen: {len(scored) - new}, new: {new}")

print("\n=== AUTO MODE, live_submit still false ===")
cfg.autonomy.mode = "auto"
if RUN_BROWSER:
    from playwright.sync_api import sync_playwright
    from jobagent.browser import _check_radio_option, fill_and_submit

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_page()
        p.set_content("""<!doctype html><fieldset><legend>Gender</legend>
          <label><input data-ja-idx="0" required type="radio"
            name="gender" value="Female">Female</label>
          <label><input data-ja-idx="1" type="radio"
            name="gender" value="Male">Male</label>
          <label><input data-ja-idx="2" type="radio"
            name="gender" value="Decline to self-identify">Decline</label>
        </fieldset>""")
        assert _check_radio_option(p, '[data-ja-idx="0"]', "gender", "Male")
        assert p.locator('input[value="Male"]').is_checked()
        assert not p.locator('input[value="Female"]').is_checked()
        b.close()

    html = """<!doctype html><html><body><form>
    <label>First Name <input required name="first_name"></label>
    <label>Email Address <input required name="email" type="email"></label>
    <label>Are you legally authorized to work in the US?
      <select required name="auth">
        <option></option><option>Yes</option><option>No</option>
      </select>
    </label>
    <label>LinkedIn Profile <input name="linkedin"></label>
    <label>Why do you want to work here?<textarea name="why"></textarea></label>
    <label>Gender
      <select required name="gender_select">
        <option></option><option>Female</option><option>Male</option>
        <option>Decline to self-identify</option>
      </select>
    </label>
    <fieldset><legend>Gender</legend>
      <label><input required type="radio" name="gender_radio" value="Female">Female</label>
      <label><input type="radio" name="gender_radio" value="Male">Male</label>
      <label><input type="radio" name="gender_radio" value="Decline to self-identify">Decline</label>
    </fieldset>
    <fieldset><legend>Work Experience</legend>
      <label>Company <input name="experience_0_company"></label>
      <label>Job Title <input name="experience_0_title"></label>
      <label>Location <input name="experience_0_location"></label>
      <label>Start Month
        <select name="experience_0_start_month">
          <option></option><option>March</option><option>September</option>
        </select>
      </label>
      <label>Start Year
        <select name="experience_0_start_year">
          <option></option><option>2024</option><option>2025</option>
        </select>
      </label>
      <label>I currently work here <input name="experience_0_current" type="checkbox"></label>
      <label>Description <textarea name="experience_0_description"></textarea></label>
      <label>Company <input name="experience_1_company"></label>
      <label>Job Title <input name="experience_1_title"></label>
      <label>Location <input name="experience_1_location"></label>
      <label>End Month
        <select name="experience_1_end_month">
          <option></option><option>August</option><option>September</option>
        </select>
      </label>
      <label>End Year
        <select name="experience_1_end_year">
          <option></option><option>2024</option><option>2025</option>
        </select>
      </label>
      <label>Description <textarea name="experience_1_description"></textarea></label>
    </fieldset>
    <label>Resume <input required name="resume" type="file"></label>
    <label>Cover Letter <input name="cover_letter" type="file"></label>
    <button type="submit">Submit application</button>
    </form></body></html>"""
    browser_dir = tempfile.mkdtemp()
    form = Path(browser_dir) / "form.html"
    form.write_text(html)
    browser_book = AnswerBook.from_text(Path("answers.example.md").read_text() + """
## Fields
- gender: Male
- hispanic_latino: "No"
- race: Asian
- veteran_status: I am not a protected veteran
- disability_status: No, I do not have a disability and have not had one in the past
""")
    cfg.apply.headless = True
    cfg.apply.screenshot_dir = str(Path(browser_dir) / "screenshots")
    local_job = Job(
        source="local", company="Smoke", title="New Grad Product Designer",
        url=form.as_uri(), apply_url=form.as_uri(), location="Remote", score=1.0,
    )
    local_cover = write_cover_letter(local_job, cfg)
    result = fill_and_submit(
        local_job, cfg, browser_book, live=False,
        cover_letter_path=local_cover,
    )
    assert result.status == "needs_review" and result.screenshot
    assert Path(result.screenshot).exists()
    filled_text = "\n".join(result.filled)
    assert "Gender = Male" in filled_text
    assert "Female" not in [
        line.split(" = ", 1)[1].split("  ", 1)[0]
        for line in result.filled if line.startswith("Gender = ")
    ]
    assert "Company = Northwind Studio" in filled_text
    assert "Company = Brightline Labs" in filled_text
    assert "Location = Remote" in filled_text
    assert "Cover Letter (file:" in filled_text and ".pdf" in filled_text
    print(f"[{result.status}] local browser dry run :: {result.note}")
else:
    import jobagent.apply as apply_mod
    from jobagent.browser import SubmitResult

    real_fill = apply_mod.fill_and_submit
    apply_mod.fill_and_submit = lambda job, cfg, book, live, **kwargs: SubmitResult(
        "needs_review",
        note="mock dry run: browser would fill fields; live_submit is false",
    )
    try:
        for j in scored[:2]:
            rec = decide(j, cfg, "", book=book)
            print(f"[{rec.status}] {j.company} - {j.title} :: {rec.note}")
    finally:
        apply_mod.fill_and_submit = real_fill

print("\n=== NEW CONNECTORS (mock payloads, no network) ===")
from jobagent.sources import adzuna, recruitee, smartrecruiters, workable
from jobagent.sources import REGISTRY

for kind in ("workable", "smartrecruiters", "recruitee", "adzuna"):
    assert kind in REGISTRY, f"{kind} missing from REGISTRY"

# --- workable: HTML stripped, application_url used, remote location ---
workable.get_json = lambda url, retries=3: {"jobs": [{
    "title": "Product Designer", "shortcode": "AB12", "department": "Design",
    "url": "https://apply.workable.com/j/AB12",
    "application_url": "https://apply.workable.com/j/AB12/apply",
    "published_on": "2026-06-01", "country": "Finland", "city": "Helsinki",
    "state": "", "telecommuting": True,
    "description": "<p>Design <b>wearables</b> &amp; companion apps</p>"}]}
w = list(workable.WorkableConnector("acme", "Acme").fetch())
assert len(w) == 1 and w[0].apply_url.endswith("/apply")
assert "<" not in w[0].description and "&amp;" not in w[0].description
assert not w[0].scan_only
print(f"  workable         ok: {w[0].title} @ {w[0].location} "
      f":: {w[0].description}")

# --- smartrecruiters: list + per-posting detail, HTML stripped ---
def _sr_fake(url, retries=3):
    if url.rstrip("/").endswith("/postings/123"):
        return {"applyUrl": "https://jobs.smartrecruiters.com/Acme/123-pd",
                "jobAd": {"sections": {"jobDescription": {
                    "title": "JD", "text": "<p>Figma &amp; prototyping</p>"}}}}
    return {"totalFound": 1, "content": [{
        "id": "123", "name": "Product Designer",
        "releasedDate": "2026-06-01",
        "location": {"city": "Berlin", "country": "de", "remote": True}}]}
smartrecruiters.get_json = _sr_fake
s = list(smartrecruiters.SmartRecruitersConnector("Acme", "Acme").fetch())
assert len(s) == 1 and s[0].apply_url.startswith("https://jobs.smartrecruiters")
assert s[0].description == "Figma & prototyping" and "Remote" in s[0].location
print(f"  smartrecruiters  ok: {s[0].title} @ {s[0].location} "
      f":: {s[0].description}")

# --- recruitee: drafts filtered, description+requirements merged ---
recruitee.get_json = lambda url, retries=3: {"offers": [
    {"id": 1, "title": "Product Designer", "status": "published",
     "careers_url": "https://acme.recruitee.com/o/pd",
     "careers_apply_url": "https://acme.recruitee.com/o/pd/c/new",
     "city": "Amsterdam", "country": "Netherlands", "remote": False,
     "published_at": "2026-06-01",
     "description": "<p>Own the design system</p>",
     "requirements": "<ul><li>Figma</li></ul>"},
    {"id": 2, "title": "Draft Role", "status": "draft"}]}
r = list(recruitee.RecruiteeConnector("acme", "Acme").fetch())
assert len(r) == 1 and r[0].apply_url.endswith("/c/new")
assert "Own the design system" in r[0].description and "Figma" in r[0].description
print(f"  recruitee        ok: {r[0].title} @ {r[0].location} "
      f":: {r[0].description}")

# --- adzuna: env gating + scan_only routing on apply URL ---
for var in (adzuna.ID_ENV, adzuna.KEY_ENV):
    os.environ.pop(var, None)
try:
    list(adzuna.AdzunaConnector("us", "Adzuna US").fetch())
    raise AssertionError("adzuna should skip without credentials")
except RuntimeError as exc:
    assert "skipped" in str(exc)
    print(f"  adzuna           ok (no creds): {exc}")

os.environ[adzuna.ID_ENV] = "test-id"
os.environ[adzuna.KEY_ENV] = "test-key"
adzuna.get_json = lambda url, retries=3: {"results": [
    {"id": 91, "title": "UX Engineer",
     "redirect_url": "https://boards.greenhouse.io/acme/jobs/1",
     "company": {"display_name": "Acme"},
     "location": {"display_name": "Seattle, WA"},
     "description": "Design systems &amp; prototyping", "created": "2026-06-01"},
    {"id": 92, "title": "Product Designer",
     "redirect_url": "https://www.adzuna.com/land/ad/92",
     "company": {"display_name": "BigCo"},
     "location": {"display_name": "Remote"},
     "description": "AI wearables", "created": "2026-06-01"}]}
a = list(adzuna.AdzunaConnector("us:designer:seattle", "Adzuna US").fetch())
assert len(a) == 2
assert not a[0].scan_only, "greenhouse apply URL should be appliable"
assert a[1].scan_only, "aggregator redirect must stay scan_only"
assert "&amp;" not in a[0].description
print(f"  adzuna           ok (routing): appliable={a[0].title}, "
      f"scan_only={a[1].title}")
for var in (adzuna.ID_ENV, adzuna.KEY_ENV):
    os.environ.pop(var, None)

print("\n=== TWO-PHASE APPROVAL / LIVENESS / PACING / HOLDS / TOKEN ===")
import io
import time as _time
from contextlib import redirect_stdout

from jobagent import web_approval as wa
from jobagent.cli import _print_holds
from jobagent.tracker import Tracker as _Tracker

# --- two-phase state machine: prepare parks clean fills, confirm submits ---
tp_queue = Path(smoke_dir) / "two-phase-queue.json"
tp_queue.write_text(json.dumps({"live_submit": True, "queue": [
    {"id": "JA-101", "company": "SmallCo", "title": "AI Product Designer",
     "score": 0.93, "location": "Remote", "source": "greenhouse",
     "url": "https://ex/a", "apply_url": "https://ex/a/apply",
     "reasons": [], "status": "pending_confirmation"},
    {"id": "JA-102", "company": "OtherCo", "title": "Design Engineer",
     "score": 0.80, "location": "Remote", "source": "ashby",
     "url": "https://ex/b", "apply_url": "https://ex/b/apply",
     "reasons": [], "status": "pending_confirmation"},
]}), encoding="utf-8")


def _mock_decide(job, cfg_, cover, book=None):
    live = cfg_.autonomy.live_submit
    unanswered = (["Years of experience with Kubernetes [required]"]
                  if job.company == "OtherCo" else [])
    status = "applied" if (live and not unanswered) else "needs_review"
    return ApplicationRecord(
        uid=job.uid, company=job.company, title=job.title, url=job.url,
        score=job.score, status=status,
        note="submitted (mock)" if status == "applied" else "dry-run fill (mock)",
        cover_letter_path=cover, location=job.location, source=job.source,
        screenshot="output/screenshots/mock.png",
        filled_fields=["First Name = Alex [field]"],
        unanswered_required=unanswered,
    )


real_decide = approval_mod.decide
real_liveness = approval_mod.check_posting_live
real_db_path, real_csv_path = cfg.db_path, cfg.csv_path
real_pause = (cfg.autonomy.batch_pause_min_seconds,
              cfg.autonomy.batch_pause_max_seconds)
real_out_dir = cfg.output_dir
cfg.db_path = str(Path(smoke_dir) / "twophase_smoke.db")
cfg.csv_path = ""
cfg.autonomy.batch_pause_min_seconds = 0
cfg.autonomy.batch_pause_max_seconds = 0
approval_mod.decide = _mock_decide
approval_mod.check_posting_live = lambda url, timeout=15: (True, "mock live")
try:
    # Phase 1: nothing submits even though the queue is stamped live.
    prepared, errors = approval_mod.prepare_all_queue_items(cfg, tp_queue)
    assert not errors
    q = json.loads(tp_queue.read_text(encoding="utf-8"))["queue"]
    assert q[0]["status"] == "pending_submit", q[0]["status"]
    assert q[1]["status"] == "needs_review" and q[1]["unanswered_required"]
    assert approval_mod.pending_submit_item_ids(tp_queue) == ["JA-101"]
    print("  prepare all     -> clean fill parked pending_submit, "
          "held fill stays needs_review, nothing submitted")

    # Phase 2: only the parked item submits, honoring the live stamp.
    confirmed, errors = approval_mod.confirm_all_queue_items(cfg, tp_queue)
    assert not errors and len(confirmed) == 1
    q = json.loads(tp_queue.read_text(encoding="utf-8"))["queue"]
    assert q[0]["status"] == "applied" and q[1]["status"] == "needs_review"
    print("  confirm all     -> pending_submit item applied, held item untouched")

    # A stale or wrong cover letter must fail before the submit path.
    stale_cover = Path(smoke_dir) / "stale-cover.txt"
    stale_cover.write_text(
        "Dear OtherCo Hiring Team,\n\n"
        "I am excited to apply for the Backend Engineer role.\n\n"
        "Warmly,\nAlex Rivera\n",
        encoding="utf-8",
    )
    tp_queue.write_text(json.dumps({"live_submit": True, "queue": [
        {"id": "JA-150", "company": "Figma",
         "title": "AI Product Designer",
         "score": 0.93, "location": "Remote", "source": "greenhouse",
         "url": "https://ex/figma", "apply_url": "https://ex/figma/apply",
         "reasons": [], "status": "pending_submit",
         "cover_letter_path": str(stale_cover)},
    ]}), encoding="utf-8")
    try:
        approval_mod.confirm_queue_item(cfg, tp_queue, "JA-150")
        raise AssertionError("stale cover letter was not blocked")
    except RuntimeError as exc:
        assert "cover letter package mismatch" in str(exc)
    print("  confirm guard   -> stale/wrong cover letter blocked before submit")

    # Liveness: a posting that closed between scan and approval is skipped.
    tp_queue.write_text(json.dumps({"live_submit": True, "queue": [
        {"id": "JA-201", "company": "GoneCo", "title": "Product Designer",
         "score": 0.9, "location": "Remote", "source": "greenhouse",
         "url": "https://ex/gone", "apply_url": "https://ex/gone/apply",
         "reasons": [], "status": "pending_confirmation"},
    ]}), encoding="utf-8")
    approval_mod.check_posting_live = (
        lambda url, timeout=15: (False, "posting returned HTTP 404"))
    rec = approval_mod.prepare_queue_item(cfg, tp_queue, "JA-201")
    assert rec.status == "closed_before_submit"
    q = json.loads(tp_queue.read_text(encoding="utf-8"))["queue"]
    assert q[0]["status"] == "closed_before_submit"
    print("  liveness check  -> closed posting skipped as closed_before_submit")

    # Holds report aggregates the unanswered required fields recorded above.
    cfg.output_dir = smoke_dir  # keep the report off the real review_queues
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_holds(cfg)
    out = buf.getvalue()
    assert "Kubernetes" in out and "OtherCo - Design Engineer" in out
    print("  jobagent holds  -> aggregates unanswered required fields")
finally:
    approval_mod.decide = real_decide
    approval_mod.check_posting_live = real_liveness
    cfg.db_path, cfg.csv_path = real_db_path, real_csv_path
    (cfg.autonomy.batch_pause_min_seconds,
     cfg.autonomy.batch_pause_max_seconds) = real_pause
    cfg.output_dir = real_out_dir

# --- check_posting_live signal detection (mocked HTTP, no network) ---
class _Resp:
    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


real_requests_get = approval_mod.requests.get
try:
    approval_mod.requests.get = lambda *a, **k: _Resp(200, "Apply now!")
    assert approval_mod.check_posting_live("https://ex/live")[0] is True
    approval_mod.requests.get = lambda *a, **k: _Resp(
        200, "This job is no longer accepting applications.")
    assert approval_mod.check_posting_live("https://ex/closed")[0] is False
    approval_mod.requests.get = lambda *a, **k: _Resp(404)
    assert approval_mod.check_posting_live("https://ex/404")[0] is False
    def _boom(*a, **k):
        raise approval_mod.requests.ConnectionError("dns down")
    approval_mod.requests.get = _boom
    assert approval_mod.check_posting_live("https://ex/flaky")[0] is True
finally:
    approval_mod.requests.get = real_requests_get
print("  posting liveness -> live / closed-text / 404 / network-flaky all correct")

# --- pacing waits between batch items, skips after the last ---
cfg.autonomy.batch_pause_min_seconds = 1
cfg.autonomy.batch_pause_max_seconds = 1
t0 = _time.time()
approval_mod._batch_pause(cfg, 0, 2)   # between items -> sleeps ~1s
mid = _time.time() - t0
approval_mod._batch_pause(cfg, 1, 2)   # after last -> no sleep
total = _time.time() - t0
(cfg.autonomy.batch_pause_min_seconds,
 cfg.autonomy.batch_pause_max_seconds) = real_pause
assert 0.9 <= mid <= 3 and total - mid < 0.5
print(f"  batch pacing    -> slept {mid:.1f}s between items, 0s after last")

# --- web panel: token rotation + two-phase buttons render ---
real_token_file = wa.TOKEN_FILE
wa.TOKEN_FILE = Path(smoke_dir) / "web-token"
real_env_token = os.environ.pop("JOBAGENT_APPROVAL_WEB_TOKEN", None)
try:
    t1 = wa.approval_token(rotate=True)
    t2 = wa.approval_token(rotate=True)
    t3 = wa.approval_token(rotate=False)
    assert t1 != t2 and t2 == t3 and len(t2) > 20
finally:
    wa.TOKEN_FILE = real_token_file
    if real_env_token is not None:
        os.environ["JOBAGENT_APPROVAL_WEB_TOKEN"] = real_env_token
page = wa.render_page(cfg, "tok", tp_queue)
assert "Submit for real" not in page  # JA-201 is closed, no confirm button
tp_queue.write_text(json.dumps({"live_submit": True, "queue": [
    {"id": "JA-301", "company": "SmallCo", "title": "AI Product Designer",
     "score": 0.93, "status": "pending_submit",
     "screenshot": "output/screenshots/mock.png",
     "url": "https://ex/a", "apply_url": "https://ex/a/apply"},
]}), encoding="utf-8")
page = wa.render_page(cfg, "tok", tp_queue)
assert "Submit for real" in page and "/shot?token=" in page
assert "Confirm All: submit 1" in page
assert "Stop Batch" in page
print("  web panel       -> token rotates; two-phase + screenshot UI renders")

print("\n=== SLASH COMMAND DISPATCHER (mocked, no Slack) ===")
slash_queue = Path(smoke_dir) / "slash-queue.json"
slash_queue.write_text(json.dumps({"live_submit": True, "queue": [
    {"id": "JA-401", "company": "SmallCo", "title": "AI Product Designer",
     "score": 0.9, "location": "Remote", "source": "greenhouse",
     "url": "https://ex/a", "apply_url": "https://ex/a/apply",
     "reasons": [], "status": "pending_confirmation"},
]}), encoding="utf-8")

real_decide = approval_mod.decide
real_liveness = approval_mod.check_posting_live
real_db_path, real_csv_path = cfg.db_path, cfg.csv_path
real_pause = (cfg.autonomy.batch_pause_min_seconds,
              cfg.autonomy.batch_pause_max_seconds)
cfg.db_path = str(Path(smoke_dir) / "slash_smoke.db")
cfg.csv_path = ""
cfg.autonomy.batch_pause_min_seconds = 0
cfg.autonomy.batch_pause_max_seconds = 0
approval_mod.decide = _mock_decide
approval_mod.check_posting_live = lambda url, timeout=15: (True, "mock live")
try:
    out = approval_mod.handle_slash_text(cfg, slash_queue, "help")
    assert "/jobagent refresh" in out
    assert "/jobagent coverage" in out
    assert "/jobagent scan" in out
    assert "read-only" in out  # scan vs refresh distinction is spelled out
    out = approval_mod.handle_slash_text(cfg, slash_queue, "status")
    assert "pending_confirmation" in out and "1" in out
    out = approval_mod.handle_slash_text(cfg, slash_queue, "stop")
    assert "Stop requested" in out
    assert approval_mod.batch_stop_requested(slash_queue)
    approval_mod.clear_batch_stop(slash_queue)
    out = approval_mod.handle_slash_text(cfg, slash_queue, "approve all")
    assert "NOT submitted" in out and "confirm all" in out
    q = json.loads(slash_queue.read_text(encoding="utf-8"))["queue"]
    assert q[0]["status"] == "pending_submit"
    out = approval_mod.handle_slash_text(cfg, slash_queue, "confirm all")
    q = json.loads(slash_queue.read_text(encoding="utf-8"))["queue"]
    assert q[0]["status"] == "applied"
    out = approval_mod.handle_slash_text(cfg, slash_queue, "skip all")
    assert "0" in out  # nothing left to skip
    out = approval_mod.handle_slash_text(cfg, slash_queue, "approve JA-999")
    assert "failed" in out  # unknown id is reported, not raised
    out = approval_mod.handle_slash_text(cfg, slash_queue, "coverage")
    assert out.startswith("```") and out.endswith("```")
    assert "Answer coverage by ATS" in out
    assert "[OK ]" in out or "[GAP]" in out

    # long holds/coverage output is no longer truncated at 3500; the caller
    # re-fences it per chunk via _reply_chunks (each chunk a valid ``` block).
    import jobagent.cli as cli_mod
    def _big_print(_cfg):
        for i in range(300):
            print(f"line {i:03d} " + "x" * 20)
    for verb, attr in (("holds", "_print_holds"),
                       ("coverage", "_print_coverage")):
        real_printer = getattr(cli_mod, attr)
        setattr(cli_mod, attr, _big_print)
        try:
            reply = approval_mod.handle_slash_text(cfg, slash_queue, verb)
            assert reply.startswith("```") and reply.endswith("```")
            assert len(reply) > 5000, f"{verb} was truncated"
            chunks = approval_mod._reply_chunks(reply, 3500)
            assert len(chunks) >= 2
            assert all(c.startswith("```\n") and c.endswith("\n```")
                       for c in chunks)
            assert all(len(c) <= 3500 for c in chunks)
        finally:
            setattr(cli_mod, attr, real_printer)

    # scan: read-only ranked view. cli binds pipeline.scan at import time,
    # so patch the name cli actually calls.
    import jobagent.cli as cli_mod
    fake_jobs = []
    for i in range(8):
        j = Job(source="greenhouse" if i % 2 == 0 else "ashby",
                company=f"ScanCo{i}", title="AI Product Designer",
                url=f"https://ex/scan{i}", location="Remote",
                scan_only=(i == 7))
        j.score, j.reasons = 0.9 - i * 0.05, ["mock reason"]
        fake_jobs.append(j)
    real_cli_scan = cli_mod.scan
    cli_mod.scan = lambda _cfg: pipeline_mod.ScanResult(
        jobs=fake_jobs, errors=[])
    try:
        out = approval_mod.handle_slash_text(cfg, slash_queue, "scan")
        assert out.startswith("```") and out.endswith("```")
        assert "Top 8 matches" in out
        assert "ScanCo0 - AI Product Designer" in out
        assert "Submit routing:" in out and "[scan-only]" in out
        out = approval_mod.handle_slash_text(cfg, slash_queue, "scan 5")
        assert "Top 5 matches" in out and "ScanCo7" not in out
    finally:
        cli_mod.scan = real_cli_scan

    # long fenced replies re-fence per chunk instead of splitting a ``` block
    long_reply = "```\n" + "\n".join(
        f"line {i} " + "x" * 60 for i in range(200)) + "\n```"
    chunks = approval_mod._reply_chunks(long_reply, 3500)
    assert len(chunks) > 1
    assert all(c.startswith("```\n") and c.endswith("\n```") for c in chunks)
    assert all(len(c) <= 3500 for c in chunks)
    assert approval_mod._reply_chunks("plain text", 3500) == ["plain text"]

    # slow verbs get an honest ack; others keep the generic one
    assert "scanning 14 boards" in approval_mod._slash_ack_text("scan")
    assert "checklist" in approval_mod._slash_ack_text("checklist")
    assert approval_mod._slash_ack_text("ashby") == (
        approval_mod._slash_ack_text("checklist"))
    assert approval_mod._slash_ack_text("help") == "jobagent help:"
    assert approval_mod._slash_ack_text("") == "jobagent help:"
    assert approval_mod._slash_ack_text("status") == (
        "jobagent: running `status`...")
finally:
    approval_mod.decide = real_decide
    approval_mod.check_posting_live = real_liveness
    cfg.db_path, cfg.csv_path = real_db_path, real_csv_path
    (cfg.autonomy.batch_pause_min_seconds,
     cfg.autonomy.batch_pause_max_seconds) = real_pause
print("  /jobagent       -> help/status/approve/confirm/skip/coverage/scan"
      " dispatch; two-phase respected; bad ids reported")

print("\n=== ASHBY CHECKLIST VERB (mocked, no network) ===")
import jobagent.ashby_form as ashby_form_mod

# The mrkdwn renderer alone: ## headers -> *bold* lines, **x** -> *x*,
# warning glyph and backticks pass through untouched.
_mrk = approval_mod._md_to_mrkdwn([
    "## 1. Replit — Design Engineer  ·  score 0.90",
    "- **Apply:** https://jobs.ashbyhq.com/replit/x",
    "- **Resume:** `de_resume.pdf`",
    "  - ⚠ Years of experience * → **pick one: 1-2 years, 3-5 years**",
])
assert _mrk.splitlines()[0] == "*1. Replit — Design Engineer  ·  score 0.90*"
assert "*Apply:*" in _mrk and "**" not in _mrk
assert "⚠" in _mrk and "`de_resume.pdf`" in _mrk
print("  _md_to_mrkdwn   -> headers bolded, ** converted, ⚠/backticks kept")

# Form shaped like the parsed ApiJobPosting fixture in
# scripts/test_ashby_form.py (Replit Design Engineer): a covered text field,
# a resume File field, and the ValueSelect "years" question the answer file
# does not cover (a guaranteed gap).
_checklist_form = [
    {"section": "Details", "label": "Full Name", "atype": "String",
     "required": True, "options": []},
    {"section": "Details", "label": "Resume", "atype": "File",
     "required": True, "options": []},
    {"section": "Questions",
     "label": "How many years of relevant professional experience do you have?",
     "atype": "ValueSelect", "required": True,
     "options": ["1-2 years", "3-5 years", "6-8 years", "8+"]},
]
_checklist_job = Job(
    source="ashby", company="Replit", title="Design Engineer",
    url="https://jobs.ashbyhq.com/replit/2f1de5f1-48c2-4104-a697-a51fe1620508",
    apply_url="https://jobs.ashbyhq.com/replit/"
              "2f1de5f1-48c2-4104-a697-a51fe1620508/application",
    external_id="2f1de5f1-48c2-4104-a697-a51fe1620508")
_checklist_job.score = 0.9

real_fetch_questions = ashby_form_mod.fetch_application_questions
real_scan = pipeline_mod.scan
real_answers_path = cfg.profile.answers_path
ashby_form_mod.fetch_application_questions = (
    lambda org, job_id, timeout=20: [dict(q) for q in _checklist_form])
pipeline_mod.scan = lambda c: pipeline_mod.ScanResult(
    jobs=[_checklist_job], errors=[])
cfg.profile.answers_path = "answers.example.md"
try:
    out = approval_mod.handle_slash_text(cfg, slash_queue, "checklist 2")
    out_alias = approval_mod.handle_slash_text(cfg, slash_queue, "ashby")
finally:
    ashby_form_mod.fetch_application_questions = real_fetch_questions
    pipeline_mod.scan = real_scan
    cfg.profile.answers_path = real_answers_path

assert "1 role" in out and "1 gap" in out          # summary counts
assert "*1. Replit — Design Engineer" in out       # mrkdwn role header
assert "⚠" in out                                  # the ValueSelect gap line
assert "**" not in out                             # markdown bold converted
assert "Full sheet:" in out and "manual_submit_checklist.md" in out
assert "*1. Replit — Design Engineer" in out_alias  # alias dispatches too
_sheet = Path(cfg.output_dir) / "manual_submit_checklist.md"
assert _sheet.exists()
_sheet_text = _sheet.read_text(encoding="utf-8")
assert "## 1. Replit — Design Engineer" in _sheet_text  # file stays markdown
out = approval_mod.handle_slash_text(cfg, slash_queue, "help")
assert "checklist" in out and "ashby" in out
print("  /jobagent       -> checklist + ashby alias post mrkdwn crib sheet;"
      " file on disk stays the source of truth; help updated")

print("\n=== STATUS COUNTS ===")
print(tracker.counts())
os.remove(cfg.db_path)
