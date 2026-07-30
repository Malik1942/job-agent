# Application Answers

# Copy this to answers.md and edit — or run `jobagent setup` to generate it.
# Three sections:
#   Fields    - short, reusable values matched to standard form fields
#   Questions - your prepared answers to common free-text questions
#   Context   - free-form background the LLM may draw on (only if use_llm_for_answers: true)
# The filler matches a form's field label to the closest thing here. If it
# cannot find a confident match for a REQUIRED field, it holds the job for you
# instead of guessing. It never invents answers.

## Fields
- first_name: Alex
- last_name: Rivera
- full_name: Alex Rivera
- email: alex.rivera@example.com
- phone: "+1 555 010 0100"
- location: Portland, OR
- state: Oregon
"Do you live in <office area> and can commit to coming in?" knockout questions. Set your own policy; remove the line to hold them for you. (Plain-text comments only — a leading # starts a new section and ends ## Fields.)
- office_area_commit: "Yes"
- linkedin: https://www.linkedin.com/in/alex-rivera-example
- website: https://alexrivera.example.com
- github: https://github.com/alex-rivera-example
- work_authorization: Authorized to work in the US
- require_sponsorship: No
- willing_to_relocate: Yes
- desired_salary: Open / competitive for the role and location
- start_date: Flexible, roughly two weeks notice
- pronouns: they/them
- phonetic_name: AL-ex ri-VAIR-ah
- how_did_you_hear: Company careers page
Voluntary EEO questions. Leave blank to decline, or set to "Decline to self-identify".
- gender: Decline to self-identify
- hispanic_latino: Decline to self-identify
- race: Decline to self-identify
- veteran_status: Decline to self-identify
- disability_status: Decline to self-identify

Structured work experience. The browser filler uses these to override ATS
resume auto-parse errors in Work Experience / Employment History sections.
- work_1_company: Northwind Studio
- work_1_title: Product Designer
- work_1_location: Portland, OR
- work_1_start_month: September
- work_1_start_year: 2024
- work_1_start_date: September 2024
- work_1_end_month: Present
- work_1_end_year: Present
- work_1_end_date: Present
- work_1_current: "Yes"
- work_1_description: Designed and shipped client web products end to end; built the studio's shared component library; ran usability tests with real customers each release.
- work_2_company: Brightline Labs
- work_2_title: UX Design Intern
- work_2_location: Remote
- work_2_start_month: March
- work_2_start_year: 2024
- work_2_start_date: March 2024
- work_2_end_month: August
- work_2_end_year: 2024
- work_2_end_date: August 2024
- work_2_current: "No"
- work_2_description: Supported user research and concept development for a consumer scheduling product; synthesized interview findings into design recommendations.

## Questions
Q: Why do you want to work here?
A: I build products end to end, and I care about work that stays clear and humane under real use. This team is doing exactly that, and I would like to contribute both the design craft and the prototyping skill to ship it.

Q: Tell us about yourself.
A: I am a product designer who prototypes in code. I shipped Fieldnote, an AI note-capture iOS app, and Wayfare, a trip-planning web app. I like holding both the felt and the legible sides of a product at once.

Q: What is your experience with design systems?
A: I have built and maintained component systems with variables, variants, and tokens, and connected them to code so design and implementation stay in sync.

Q: Describe a project you are proud of.
A: Fieldnote, an AI note-capture app. It makes a messy, in-the-moment habit legible without flattening it into a chore, which is the throughline in how I design.

Q: What are your salary expectations?
A: I am open and flexible, and happy to align with the band for the role and location.

## Context
Product designer who prototypes in code. Shipped Fieldnote, an AI note-capture
iOS app, and Wayfare, a trip-planning web app; explored wearable interaction
with the Pulse habit-tracker concept. Comfortable running user research and
building working prototypes. Enjoys hiking and film photography.
