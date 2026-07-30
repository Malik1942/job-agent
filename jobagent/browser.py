"""The actual ATS form filler (Playwright).

Reads the form on job.apply_url, resolves every field against your AnswerBook,
fills what it can, attaches your resume, and screenshots the result. It clicks
submit only when live_submit is on. It never touches a CAPTCHA: the moment it
sees one it stops and hands the job back to you.

This is best effort. ATS forms vary a lot and some use custom widgets that a
generic filler cannot drive reliably. That is exactly why the tool screenshots
every fill and holds any job with an unanswered required field instead of
submitting a guess. Watch it run (headless: false) the first several times.

Playwright is imported lazily so the rest of the package works without it.
Install with:  pip install playwright && playwright install chromium
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .answers import US_STATE_ABBR, US_STATE_FULL, AnswerBook, Resolved
from .config import Config
from .coverletter import ensure_cover_letter_pdf, validate_cover_letter_package
from .models import Job

logger = logging.getLogger(__name__)


class CaptchaEncountered(Exception):
    """Raised when a CAPTCHA / bot challenge appears. We stop, never solve."""


@dataclass
class SubmitResult:
    status: str                 # applied | needs_review | error
    note: str = ""
    screenshot: str = ""
    filled: list[str] = field(default_factory=list)
    unanswered_required: list[str] = field(default_factory=list)


# JavaScript that tags every form control and returns a structured description
# of it, including the best label text it can find. Runs in the page once.
_EXTRACT_JS = r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden'
        || Number(style.opacity || 1) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4;
  };
  const labelFor = (el) => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const group = el.closest('[role="group"][aria-labelledby]');
    if (group) {
      const groupLabel = document.getElementById(
        group.getAttribute('aria-labelledby'));
      if (groupLabel && groupLabel.innerText.trim()) return groupLabel.innerText;
    }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const n = document.getElementById(lb); if (n) return n.innerText; }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return l.innerText;
    }
    let p = el.closest('label'); if (p) return p.innerText;
    // nearest preceding label-ish text
    let node = el.closest('div,li,fieldset,section') || el.parentElement;
    for (let i = 0; node && i < 3; i++, node = node.parentElement) {
      const lab = node.querySelector('label,legend');
      if (lab && lab.innerText.trim()) return lab.innerText;
    }
    return el.getAttribute('placeholder') || el.getAttribute('name') || '';
  };
  const optionTextFor = (el) => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && l.innerText.trim()) return l.innerText;
    }
    const wrap = el.closest('label');
    if (wrap && wrap.innerText.trim()) return wrap.innerText;
    return el.value || '';
  };
  const groupLabelFor = (el) => {
    const roleGroup = el.closest('[role="group"][aria-labelledby]');
    if (roleGroup) {
      const groupLabel = document.getElementById(
        roleGroup.getAttribute('aria-labelledby'));
      if (groupLabel && groupLabel.innerText.trim()) return groupLabel.innerText;
    }
    const fieldset = el.closest('fieldset');
    if (fieldset) {
      const legend = fieldset.querySelector('legend');
      if (legend && legend.innerText.trim()) return legend.innerText;
    }
    const container = el.closest(
      '.ashby-application-form-field-entry, [data-field-path], [role="group"]');
    if (container) {
      const lab = container.querySelector('legend, label');
      if (lab && lab.innerText.trim()) return lab.innerText;
    }
    return '';
  };
  const contextFor = (el) => {
    const node = el.closest(
      'fieldset, section, [data-field-path], [data-testid], [class*="experience" i], [id*="experience" i], div'
    );
    if (!node) return '';
    return (node.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 600);
  };
  // Returns WHICH signal marks a field required (or 'none'). Keeping the
  // signal, not just a boolean, lets the diagnostics tell a required-detection
  // miss apart from a re-render timing miss. Same rules as _REQUIRED_FIELDS_JS.
  const requiredEvidenceFor = (el, label) => {
    if (el.required) return 'el.required';
    if (el.getAttribute('aria-required') === 'true') return 'aria-required';
    const container = el.closest(
      '.ashby-application-form-field-entry, [data-field-path], fieldset, [role="group"]');
    if (container && container.getAttribute('aria-required') === 'true')
      return 'aria-required';
    if ((label || '').includes('*')) return 'asterisk';
    const lab = container && container.querySelector('label,legend');
    const cls = lab && typeof lab.className === 'string' ? lab.className : '';
    if (cls.toLowerCase().includes('required')) return 'class';
    return 'none';
  };
  const out = [];
  let idx = 0;
  const controls = document.querySelectorAll('input, textarea, select');
  controls.forEach((el) => {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button'].includes(type)) return;
    const meta = `${el.name || ''} ${el.id || ''} ${el.className || ''}`.toLowerCase();
    if (meta.includes('captcha')) return;
    if (el.disabled || el.getAttribute('aria-hidden') === 'true') return;
    let kind = 'text';
    if (el.getAttribute('role') === 'combobox') kind = 'combobox';
    else if (el.tagName.toLowerCase() === 'select') kind = 'select';
    else if (type === 'file') kind = 'file';
    else if (type === 'checkbox') kind = 'checkbox';
    else if (type === 'radio') kind = 'radio';
    else if (el.tagName.toLowerCase() === 'textarea') kind = 'textarea';
    const buttonTexts = Array.from(
      (el.parentElement || document).querySelectorAll('button')
    ).map(b => b.innerText.trim()).filter(Boolean);
    const isYesNo = kind === 'checkbox'
      && buttonTexts.some(t => /^yes$/i.test(t))
      && buttonTexts.some(t => /^no$/i.test(t));
    if (isYesNo) kind = 'yesno';
    if (kind !== 'file' && kind !== 'yesno' && kind !== 'combobox'
        && !visible(el)) return;
    el.setAttribute('data-ja-idx', idx);
    let options = [];
    if (kind === 'select') options = Array.from(el.options).map(o => o.textContent.trim()).filter(Boolean);
    if (kind === 'yesno') options = ['Yes', 'No'];
    if (kind === 'radio') {
      const group = el.name
        ? document.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.name)}"]`)
        : [el];
      options = Array.from(group).map(optionTextFor).map(
        t => (t || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
    }
    const rawLabel = kind === 'radio' ? (groupLabelFor(el) || labelFor(el)) : labelFor(el);
    const label = (rawLabel || '').replace(/\s+/g, ' ').trim();
    if (kind === 'combobox'
        && /authorized|sponsor|sponsorship|work in/i.test(label)) {
      options = ['Yes', 'No'];
    }
    let evidence = requiredEvidenceFor(el, label);
    let required = evidence !== 'none';
    // Consent / agreement checkboxes gate submission even when not marked
    // required in the HTML (Ashby's data-retention box, privacy-policy
    // acknowledgements). Treat them as required so they are either answered
    // from the answers file (consent: Yes) or held for the human.
    if (kind === 'checkbox'
        && /i agree|consent|acknowledg|privacy policy|retain (my|your) data|terms/i
           .test(`${label} ${contextFor(el)}`)) {
      required = true;
      if (evidence === 'none') evidence = 'consent-heuristic';
    }
    out.push({ idx, kind, type, name: el.name || '', id: el.id || '',
               placeholder: el.getAttribute('placeholder') || '',
               label, context: contextFor(el), options, required, evidence });
    idx++;
  });
  return out;
}
"""

_CAPTCHA_JS = r"""
() => {
  const sel = [
    'iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]',
    'iframe[src*="turnstile"]', '.g-recaptcha', '[data-sitekey]',
    'iframe[title*="captcha" i]', '#px-captcha',
  ];
  const visible = (el) => {
    const src = (el.getAttribute('src') || '').toLowerCase();
    if (src.includes('recaptcha') && src.includes('/anchor?')
        && src.includes('size=invisible')) return false;
    if (src.includes('recaptcha') && src.includes('/anchor?')) return false;
    if (src.includes('invisible')) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden'
        || Number(style.opacity || 1) === 0) return false;
    const r = el.getBoundingClientRect();
    const inViewport = r.bottom > 0 && r.right > 0
      && r.top < window.innerHeight && r.left < window.innerWidth;
    return r.width > 4 && r.height > 4 && inViewport;
  };
  return sel.some(s => Array.from(document.querySelectorAll(s)).some(visible));
}
"""

# Option nodes rendered by autocomplete/combobox widgets. Covers native
# listboxes, Greenhouse's new job boards, react-select (incl. portals
# attached to <body>), and generic *-option-* ids.
_OPTION_SELECTOR = (
    '[role="listbox"] [role="option"], [role="option"], '
    '[id*="-option-"], [class*="select__option"], '
    'ul[class*="autocomplete" i] li, ul[class*="suggestions" i] li'
)


def _pick_option_index(texts: list[str], value: str) -> int | None:
    """Rank visible dropdown options against the desired value.
    Exact match > prefix > containment > first-token match (so 'Seattle, WA'
    happily selects 'Seattle, Washington, United States')."""
    want = value.strip().lower()
    if not want:
        return None
    first_token = want.split(",")[0].strip()
    ranked: list[tuple[int, int]] = []  # (score, index)
    for i, raw in enumerate(texts):
        t = " ".join((raw or "").split()).lower()
        if not t or "no results" in t or "no options" in t:
            continue
        if t == want:
            ranked.append((100, i))
        elif t.startswith(want) or want.startswith(t):
            ranked.append((80, i))
        elif want in t:
            ranked.append((60, i))
        elif first_token and t.startswith(first_token):
            ranked.append((40, i))
    if not ranked:
        return None
    ranked.sort(key=lambda p: (-p[0], p[1]))
    return ranked[0][1]


def _fill_combobox(page, sel: str, value: str, strict: bool = False) -> str:
    """Drive an autocomplete/combobox like a person: type, wait for the
    suggestion list, click the best match. Raw text left in one of these
    widgets does not count as an answer — Greenhouse's location field is the
    canonical example. Returns the text of the option actually selected."""
    loc = page.locator(sel).first
    loc.click(force=True)
    try:
        loc.fill("")
    except Exception:
        pass  # some react-select inputs reject fill(); typing still works
    loc.press_sequentially(value[:60], delay=40)

    picked = ""
    try:
        def _visible_options():
            # Only consider VISIBLE options — stale listboxes from other
            # fields linger in the DOM display:none and must not pollute
            # matching.
            allopt = page.locator(_OPTION_SELECTOR)
            out = []
            for i in range(min(allopt.count(), 800)):
                nth = allopt.nth(i)
                try:
                    if nth.is_visible():
                        out.append((nth, nth.inner_text().strip()))
                except Exception:
                    continue
                if len(out) >= 15:
                    break
            return out

        # Poll for a VISIBLE option. wait_for_selector cannot be used here:
        # with a comma-list selector it binds to the first DOM match, which
        # may be a hidden leftover option from another field's listbox.
        deadline = time.time() + 5.0
        options = []
        while time.time() < deadline:
            options = _visible_options()
            if options:
                break
            page.wait_for_timeout(150)
        if options:
            page.wait_for_timeout(350)  # let async suggestions settle
            options = _visible_options()
        idx = _pick_option_index([t for _, t in options], value)
        if idx is None:
            # Retry with only the first token — geo autocompletes dislike
            # state abbreviations, and long sentence answers ("Yes, I ...")
            # filter a short option list ("Yes") down to nothing.
            token = re.split(r"[,.]", value)[0].strip()
            if token and token.lower() != value.strip().lower():
                loc.fill("")
                loc.press_sequentially(token, delay=40)
                page.wait_for_timeout(700)
                options = _visible_options()
                idx = _pick_option_index([t for _, t in options], token)
        if idx is None:
            # US state retry: Greenhouse State comboboxes often list ONLY
            # abbreviations ("WA"), so typing "Washington" filters the list
            # to nothing. Try the mapped form (both directions).
            nval = _norm(value)
            alt = US_STATE_ABBR.get(nval) or US_STATE_FULL.get(nval)
            if alt:
                typed = alt.upper() if len(alt) == 2 else alt.title()
                loc.fill("")
                loc.press_sequentially(typed, delay=40)
                page.wait_for_timeout(700)
                options = _visible_options()
                idx = _pick_option_index([t for _, t in options], typed)
        if idx is not None:
            picked = options[idx][1]
            options[idx][0].click()
        else:
            if strict:
                raise RuntimeError(f"no exact option for {value!r}")
            raise RuntimeError("no matching option")
    except Exception:
        if strict:
            raise
        # Last resort: highlight the first suggestion and commit it.
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        picked = "(first suggestion via keyboard)"
    page.wait_for_timeout(250)
    if picked == "(first suggestion via keyboard)":
        # Do not trust the blind keyboard commit: read back what the widget
        # actually holds. An empty control here means the fill FAILED and
        # must surface as unanswered, not get logged as success. The
        # single-value lookup is scoped to THIS field's own control (a shared
        # ancestor would read a filled sibling combobox's value).
        actual = page.evaluate(r"""
          (sel) => {
            const el = document.querySelector(sel);
            if (!el) return '';
            if (el.value && el.value.trim()) return el.value.trim();
            const wrap = el.closest(
              '[class*="control" i], [class*="value-container" i], '
              + '[class*="valuecontainer" i]');
            if (wrap) {
              const picked = wrap.querySelector(
                '[class*="singleValue" i], [class*="single-value" i]');
              const t = picked && (picked.innerText || '').trim();
              if (t) return t;
            }
            return '';
          }
        """, sel)
        if not actual or _is_placeholder_wording(actual):
            # Placeholder-worded readback ("Select department") means the
            # blind commit landed on the prompt, not an option — that is a
            # failed fill, never a success to log.
            raise RuntimeError(
                f"combobox fill did not register (wanted {value!r}, "
                f"widget shows {actual!r})")
        # The blind commit must also have landed on something RELATED to the
        # wanted value ("Seattle, WA" -> "Seattle, Washington, United
        # States"), not an arbitrary first option ("Yes" for a typed-out
        # relocation answer). Unrelated readback = failed fill, hold it.
        nact, nwant = _norm(actual), _norm(value)
        first = nwant.split(",")[0].split()[0] if nwant.split() else ""
        related = bool(
            nact and nwant
            and (nact in nwant or nwant in nact
                 or (first and len(first) >= 3 and nact.startswith(first)))
        )
        if not related:
            raise RuntimeError(
                f"combobox fill committed an unrelated option (wanted "
                f"{value!r}, widget shows {actual!r})")
        picked = actual
    return picked


def _never_fill(label: str, cfg: Config) -> bool:
    """Fields the tool must not answer (portfolio passwords, access codes).
    Filling these with a guessed value reads as junk to a recruiter; a human
    decides what — if anything — goes there."""
    nlabel = _norm(label)
    return any(_norm(k) in nlabel for k in cfg.apply.never_fill_keywords)


def _is_sensitive_choice(label: str) -> bool:
    nlabel = _norm(label)
    return any(
        marker in nlabel
        for marker in (
            "gender", "hispanic", "latino", "race", "ethnicity",
            "veteran", "disability", "authorized", "sponsor",
            "worked for figma before", "worked for this company before",
        )
    )


def _is_cover_letter_text_field(label: str, context: str, kind: str) -> bool:
    if kind not in {"textarea", "text"}:
        return False
    text = _norm(f"{label} {context}")
    return any(
        marker in text
        for marker in (
            "cover letter", "additional information",
            "anything else you d like to share", "anything else you would like",
            "additional comments", "additional info",
        )
    )


def _read_cover_letter(path: str) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="ignore").strip()


def _field_signature(f: dict) -> tuple[str, str, str, str, str]:
    return (
        str(f.get("kind") or ""),
        str(f.get("name") or ""),
        str(f.get("id") or ""),
        str(f.get("label") or ""),
        str(f.get("placeholder") or ""),
    )


_VERIFY_FILLED_JS = r"""
(checks) => {
  const norm = (s) => (s || '').toLowerCase()
    .replace(/[^a-z0-9 ]/g, ' ').replace(/\s+/g, ' ').trim();
  const textForRadio = (el) => {
    if (!el) return '';
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab && lab.innerText.trim()) return lab.innerText.trim();
    }
    const wrap = el.closest('label');
    if (wrap && wrap.innerText.trim()) return wrap.innerText.trim();
    return el.value || '';
  };
  const comboText = (el) => {
    // Scope to THIS field's own react-select control so a filled sibling
    // combobox under a shared ancestor is never misread as this field's value.
    const wrap = el.closest(
      '[class*="control" i], [class*="value-container" i], '
      + '[class*="valuecontainer" i]');
    if (wrap) {
      const picked = wrap.querySelector(
        '[class*="singleValue" i], [class*="single-value" i]');
      const t = picked && (picked.innerText || picked.textContent || '').trim();
      if (t) return t;
      const ct = (wrap.innerText || '').trim();
      if (ct && ct.length < 120) return ct;
    }
    return el.value || el.getAttribute('value') || '';
  };
  const valueFor = (el, kind) => {
    if (!el) return '';
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    if (tag === 'select') {
      return el.options[el.selectedIndex]?.textContent?.trim() || el.value || '';
    }
    if (type === 'radio') {
      const group = el.name
        ? Array.from(document.querySelectorAll(
            `input[type="radio"][name="${CSS.escape(el.name)}"]`))
        : [el];
      const checked = group.find(x => x.checked);
      return textForRadio(checked);
    }
    if (type === 'checkbox') return el.checked ? 'Yes' : 'No';
    if (kind === 'combobox' || el.getAttribute('role') === 'combobox') {
      return comboText(el);
    }
    return el.value || '';
  };
  return checks.map((check) => {
    const el = document.querySelector(`[data-ja-idx="${check.idx}"]`);
    return {...check, actual: valueFor(el, check.kind || '')};
  });
}
"""


def _value_matches(expected: str, actual: str, strict: bool = False) -> bool:
    nexp = _norm(expected)
    nact = _norm(actual)
    if not nexp:
        return True
    if not nact:
        return False
    if strict:
        if nexp == "male":
            return nact == "male" or nact == "i identify as a man"
        if nexp == "female":
            return nact == "female" or nact == "i identify as a woman"
        return nexp == nact or nexp in nact
    if nexp == nact or nexp in nact or nact in nexp:
        return True
    first = nexp.split()[0] if nexp.split() else ""
    return bool(first and len(first) >= 4 and nact.startswith(first))


# Fresh-DOM gather of every REQUIRED (or aria-invalid) control's raw state.
# This JS only GATHERS; the pass/fail decision lives in Python
# (presubmit_verdict) so it can be unit-tested with no browser. The
# required-evidence rules are kept identical to _EXTRACT_JS on purpose — one
# rule set, so the diagnostics and the invariant never disagree.
_REQUIRED_FIELDS_JS = r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden'
        || Number(style.opacity || 1) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4;
  };
  const labelFor = (el) => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map(id => {
        const n = document.getElementById(id);
        return n ? n.innerText : '';
      }).join(' ').trim();
      if (t) return t;
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && l.innerText.trim()) return l.innerText;
    }
    const wrap = el.closest('label');
    if (wrap && wrap.innerText.trim()) return wrap.innerText;
    const node = el.closest('fieldset, [role="group"], [data-field-path], div');
    const lab = node && node.querySelector('legend, label');
    if (lab && lab.innerText.trim()) return lab.innerText;
    return el.getAttribute('placeholder') || el.name || el.id || '(unlabeled)';
  };
  const contextFor = (el) => {
    const node = el.closest(
      'fieldset, section, [data-field-path], [role="group"], div');
    return node
      ? (node.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 400) : '';
  };
  // Same signal order as _EXTRACT_JS.requiredEvidenceFor.
  const requiredEvidenceFor = (el, label) => {
    if (el.required) return 'el.required';
    if (el.getAttribute('aria-required') === 'true') return 'aria-required';
    const container = el.closest(
      '.ashby-application-form-field-entry, [data-field-path], fieldset, [role="group"]');
    if (container && container.getAttribute('aria-required') === 'true')
      return 'aria-required';
    if ((label || '').includes('*')) return 'asterisk';
    const lab = container && container.querySelector('label,legend');
    const cls = lab && typeof lab.className === 'string' ? lab.className : '';
    if (cls.toLowerCase().includes('required')) return 'class';
    return 'none';
  };
  // Read a react-select style combobox's chosen text, falling back to the
  // input's own value (plain role=combobox inputs keep the selection there).
  // Scope the single-value lookup to THIS field's own control — climbing to a
  // shared ancestor and taking the first match read a FILLED sibling
  // combobox's value and passed an empty required field (false-negative). The
  // single-value node always lives inside this input's react-select control.
  const comboText = (el) => {
    const wrap = el.closest(
      '[class*="control" i], [class*="value-container" i], '
      + '[class*="valuecontainer" i]');
    if (wrap) {
      const picked = wrap.querySelector(
        '[class*="singleValue" i], [class*="single-value" i]');
      const t = picked && (picked.innerText || picked.textContent || '').trim();
      if (t) return t;
    }
    return (el.value || el.getAttribute('value') || '').trim();
  };
  const seen = new Set();
  const out = [];
  let idx = 0;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button'].includes(type)) continue;
    const meta =
      `${el.name || ''} ${el.id || ''} ${el.className || ''}`.toLowerCase();
    if (meta.includes('captcha')) continue;
    if (el.disabled || el.getAttribute('aria-hidden') === 'true') continue;

    let kind = 'text';
    if (el.getAttribute('role') === 'combobox') kind = 'combobox';
    else if (el.tagName.toLowerCase() === 'select') kind = 'select';
    else if (type === 'file') kind = 'file';
    else if (type === 'checkbox') kind = 'checkbox';
    else if (type === 'radio') kind = 'radio';
    else if (el.tagName.toLowerCase() === 'textarea') kind = 'textarea';

    // File inputs are usually visually hidden behind a styled button, so the
    // generic hidden-element skip must NOT silence a required upload check.
    // Comboboxes can also render a zero-size input; keep them too.
    if (kind !== 'file' && kind !== 'combobox' && !visible(el)) continue;

    const label = (labelFor(el) || '').replace(/\s+/g, ' ').trim();
    let evidence = requiredEvidenceFor(el, label);
    let required = evidence !== 'none';
    const ariaInvalid = el.getAttribute('aria-invalid') === 'true';
    // Consent / agreement checkboxes gate submission even without a required
    // attribute — same heuristic as the fill-time scan.
    if (kind === 'checkbox'
        && /i agree|consent|acknowledg|privacy policy|retain (my|your) data|terms/i
           .test(`${label} ${contextFor(el)}`)) {
      required = true;
      if (evidence === 'none') evidence = 'consent-heuristic';
    }
    if (!required && !ariaInvalid) continue;

    // A radio group is one logical field: dedupe by type+name. Checkboxes are
    // NOT deduped by name — each checkbox is its own field (a consent box and
    // an unrelated same-name box must not collapse into one record), and its
    // state is read from the element itself below.
    const groupKey = kind === 'radio' && el.name ? `radio:${el.name}` : '';
    if (groupKey) {
      if (seen.has(groupKey)) continue;
      seen.add(groupKey);
    }

    let value = '', selectedLabel = '', comboTextVal = '';
    let checked = false, fileCount = 0;
    let selectedIndex = -1, selectedDisabled = false;
    if (kind === 'select') {
      value = el.value || '';
      const opt = el.options[el.selectedIndex];
      selectedLabel = opt ? (opt.textContent || '').trim() : '';
      selectedIndex = el.selectedIndex;
      selectedDisabled = opt ? !!opt.disabled : false;
    } else if (kind === 'combobox') {
      comboTextVal = comboText(el);
    } else if (kind === 'radio') {
      // Scope to the radio group by type+name (matches _EXTRACT_JS) so a
      // same-name non-radio control can never satisfy the check.
      const group = el.name
        ? document.querySelectorAll(
            `input[type="radio"][name="${CSS.escape(el.name)}"]`)
        : [el];
      checked = Array.from(group).some(x => x.checked);
    } else if (kind === 'checkbox') {
      // This checkbox only. Reading a same-name sibling's checked state was a
      // false-negative: an unchecked required consent box next to any checked
      // same-name control would have passed.
      checked = !!el.checked;
    } else if (kind === 'file') {
      fileCount = el.files ? el.files.length : 0;
    } else {
      value = el.value || '';
    }

    out.push({ idx, label, kind, required, evidence,
               aria_invalid: ariaInvalid, value,
               selected_label: selectedLabel, selected_index: selectedIndex,
               selected_disabled: selectedDisabled, combo_text: comboTextVal,
               placeholder: el.getAttribute('placeholder')
                 || el.getAttribute('aria-placeholder') || '',
               checked, file_count: fileCount });
    idx++;
    if (out.length >= 60) break;   // safety cap on runaway forms
  }
  return out;
}
"""

# A native-select option or combobox readback that still reads as the "pick
# one" placeholder counts as empty — the exact class the old sweep (value===""
# only) let slip through on Greenhouse's State field.
#
# Telling a placeholder ("Select gender", "Choose one", "-- Select --") apart
# from a real answer that merely STARTS with the same verb ("Select Portfolio
# Servicing", "Choose Chicago", "Choose not to disclose") needs more than a
# prefix regex — a bare prefix over-holds real names, and a fixed continuation
# allowlist under-holds the huge "Select <bare noun>" class. So the decision in
# _is_placeholder_wording uses three signals after the verb:
#   1. decline-ish text is always a real answer (EEO "Choose not to disclose")
#   2. an all-lowercase short continuation is a placeholder ("Select gender") —
#      real proper-noun answers capitalize ("Select Portfolio Servicing")
#   3. a generic-noun first word is a placeholder in any case ("Select Gender",
#      "Choose An Option") — proper nouns are not in the set ("Chicago")
_PLACEHOLDER_DASH_RE = re.compile(
    r"^(?:--+|—+)\s*(?:select|choose|pick|$)", re.I)
_PLACEHOLDER_PLEASE_RE = re.compile(
    r"^please\b[^.]*\b(?:select|choose|pick|specify|make a)", re.I)
_PLACEHOLDER_VERB_RE = re.compile(
    r"^(select|choose|pick)\b[\s:]*(.*)$", re.I)
_PLACEHOLDER_DECLINE_RE = re.compile(
    r"\b(?:decline|prefer not|not to|rather not|no answer"
    r"|do not wish|don t wish|wish (?:not )?to answer)\b", re.I)
# Generic nouns that follow the verb in real ATS placeholder wordings. Proper
# nouns (company/city names) are deliberately absent so "Select Portfolio
# Servicing" / "Choose Chicago" read as real answers.
_PLACEHOLDER_GENERIC_WORDS = {
    "a", "an", "one", "your", "the", "from", "all", "any", "below",
    "option", "options", "item", "items", "value", "answer", "response",
    "state", "country", "city", "region", "province", "location",
    "gender", "ethnicity", "race", "veteran", "disability", "status",
    "pronoun", "pronouns", "department", "team", "role", "title",
    "position", "office", "file", "type", "category", "level",
    "source", "referral", "degree", "school", "university", "language",
    "currency", "salary", "year", "month", "day", "date", "time",
    "reason", "preference",
}


def _is_placeholder_wording(text: str) -> bool:
    """Does this option label / combobox readback read as an UNANSWERED
    placeholder prompt rather than a chosen answer? Pure text decision, shared
    by the select and combobox verdict branches (and _fill_combobox's blind-
    commit guard) so all placeholder judgments agree."""
    t = " ".join((text or "").split())
    if not t:
        return False
    if _PLACEHOLDER_DASH_RE.match(t):
        return True
    if _PLACEHOLDER_PLEASE_RE.match(t):
        return True
    m = _PLACEHOLDER_VERB_RE.match(t)
    if not m:
        return False
    # Decline-style answers legitimately start with the verb ("Choose not to
    # disclose") — always a real answer, never a placeholder.
    if _PLACEHOLDER_DECLINE_RE.search(t):
        return False
    rest = m.group(2).strip(" .…:*-")
    if not rest:
        return True  # "Select", "Select…", "Choose:"
    words = rest.split()
    if len(words) <= 4:
        # "Select gender", "select all that apply" — placeholders are
        # sentence-case; a proper-noun answer capitalizes its words.
        if all(not w[:1].isupper() for w in words):
            return True
        # "Select Gender", "Choose An Option" — title-cased placeholders
        # still lead with a generic noun, which no real proper-noun does.
        if words[0].lower().strip(".…:,") in _PLACEHOLDER_GENERIC_WORDS:
            return True
    return False


# Option VALUES that a placeholder option carries in the wild ("", or a sentinel
# that is almost never a real answer). Deliberately EXCLUDES "0"/"1": those are
# common legitimate values ("0-1 years"), so an index-0 "0" is not treated as a
# placeholder on value alone.
_PLACEHOLDER_SELECT_VALUES = {"none", "null", "select", "choose", "placeholder",
                              "-1", "undefined", "default"}


def _required_field_unfilled(rec: dict) -> str | None:
    """Pure decision: is this one required field still unfilled? Returns a short
    reason string when it is (for logging + holding), else None.

    Operates ONLY on the raw state gathered by _REQUIRED_FIELDS_JS. It never
    touches the page and never fills — its only outputs are "unfilled + why" or
    None. This is the unit-testable core of the pre-submit invariant.
    """
    kind = str(rec.get("kind") or "")
    if rec.get("aria_invalid"):
        return "aria-invalid"
    if kind in ("text", "textarea"):
        return None if str(rec.get("value") or "").strip() else "empty"
    if kind == "select":
        value = str(rec.get("value") or "").strip()
        label = str(rec.get("selected_label") or "").strip()
        if not value:
            return "no option selected"
        # A selected option marked disabled is the HTML idiom for a placeholder
        # (`<option disabled selected>Choose…</option>`) — structural, so it
        # catches placeholders whose wording the regex would miss.
        if rec.get("selected_disabled"):
            return f"placeholder option selected ({label!r})"
        if _is_placeholder_wording(label):
            return f"placeholder option selected ({label!r})"
        # The first option carrying a sentinel value ("none"/"null"/…) is a
        # placeholder even when its LABEL is worded oddly or non-English
        # ("Pick one", "Choisir") — the class the wording check alone missed.
        # (Explicit None check: `0 or -1` would wrongly collapse index 0.)
        sel_index = rec.get("selected_index")
        sel_index = -1 if sel_index is None else int(sel_index)
        if sel_index == 0 and value.lower() in _PLACEHOLDER_SELECT_VALUES:
            return f"placeholder option selected ({label!r})"
        return None
    if kind == "combobox":
        text = str(rec.get("combo_text") or "").strip()
        if not text:
            return "empty"
        # Structural backstop: readback equal to the input's own placeholder
        # attribute means nothing was chosen, whatever the wording says.
        ph = " ".join(str(rec.get("placeholder") or "").split()).lower()
        if ph and " ".join(text.split()).lower() == ph:
            return f"placeholder text ({text!r})"
        if _is_placeholder_wording(text):
            return f"placeholder text ({text!r})"
        return None
    # _REQUIRED_FIELDS_JS classifies a yes/no button-pair widget's backing
    # control as a plain "checkbox" (it has no button-pair detection, unlike
    # _EXTRACT_JS), so "yesno" is defensive here; both resolve on checked state.
    if kind in ("checkbox", "radio", "yesno"):
        return None if rec.get("checked") else "not checked"
    if kind == "file":
        return None if int(rec.get("file_count") or 0) > 0 else "no file attached"
    # Unknown kind: do not fabricate a failure for a control we cannot reason
    # about — that would be a false hold. Treat it as filled.
    return None


def presubmit_verdict(records: list[dict]) -> list[dict]:
    """Pure verdict over gathered required-field state (the JS gathers, this
    decides). Returns one structured record per still-unfilled required field:
    {label, kind, required, evidence, reason}. Empty list == safe to submit."""
    out: list[dict] = []
    for rec in records or []:
        if not (rec.get("required") or rec.get("aria_invalid")):
            continue
        reason = _required_field_unfilled(rec)
        if reason:
            out.append({
                "label": str(rec.get("label") or "(unlabeled)"),
                "kind": str(rec.get("kind") or ""),
                "required": bool(rec.get("required")),
                "evidence": str(rec.get("evidence") or "none"),
                "reason": reason,
            })
    return out


def _format_presubmit_fail(f: dict, suffix: str = "") -> str:
    """One human-readable line for a failing field, carried in the tracker's
    unanswered_required so `jobagent holds` can aggregate it."""
    tail = f"presubmit: {f.get('reason', 'empty')}"
    if suffix:
        tail = f"{tail}; {suffix}"
    return (f"{f.get('label') or '(unlabeled)'} "
            f"[{f.get('kind') or '?'}, required via {f.get('evidence') or 'none'}] "
            f"({tail})")


def _gather_presubmit_failures(page) -> list[dict]:
    """Fresh-DOM gather + pure verdict. Raises on an evaluate failure so each
    caller picks its own fail policy (fail-open for the diagnostic post-fill
    sweep, fail-closed for the submit-boundary invariant)."""
    records = page.evaluate(_REQUIRED_FIELDS_JS)
    return presubmit_verdict(records)


# Post-submit verification: did the page actually accept the application?
# Returns {confirmed, evidence, form_present, errors: [...]}. Confirmation
# phrases cover Greenhouse, Ashby, Lever, Workable, SmartRecruiters, and
# Recruitee success screens; error harvesting grabs visible validation text.
_SUBMIT_VERIFY_JS = r"""
() => {
  const bodyText = (document.body.innerText || '').toLowerCase();
  const confirmPhrases = [
    'thank you for applying', 'thanks for applying',
    'application submitted', 'application was submitted',
    'application has been submitted', 'application received',
    'application was received', "we've received your application",
    'we have received your application', 'successfully submitted',
    'your application to', 'thank you for your interest',
    'thank you for your application',
  ];
  const hit = confirmPhrases.find(p => bodyText.includes(p));

  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden'
        || Number(style.opacity || 1) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4;
  };

  // Is an application form still on screen (a visible submit control)?
  const submitBtn = Array.from(document.querySelectorAll(
    'button, input[type=submit]')).find(x =>
      /submit|apply\b|send application/i.test(x.innerText || x.value || '')
      && visible(x));

  // Harvest visible validation errors near form fields.
  const errSel = [
    '[class*="error" i]', '[class*="invalid" i]', '[role="alert"]',
    '[aria-invalid="true"] ~ *', '.field-error', '.helper-text',
  ];
  const seen = new Set();
  const errors = [];
  // Page-level rejection banners (Ashby's "flagged as possible spam",
  // generic "something went wrong") are the clearest failure evidence —
  // capture them even though they sit far from any field.
  const rejectRe = /couldn.?t submit|could not submit|unable to submit|flagged as possible spam|possible spam|something went wrong|try (again|submitting)|submission (failed|was flagged)/i;
  const fieldRe = /required|invalid|please (enter|select|choose|share|pick)|must /i;
  for (const s of errSel) {
    for (const el of document.querySelectorAll(s)) {
      if (!visible(el)) continue;
      const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
      if (!t || t.length > 300 || seen.has(t)) continue;
      if (rejectRe.test(t) || (t.length <= 120 && fieldRe.test(t))) {
        seen.add(t); errors.push(t.slice(0, 200));
      }
      if (errors.length >= 12) break;
    }
    if (errors.length >= 12) break;
  }
  // The banner may not live under an error-ish selector at all; fall back
  // to scanning the page text once.
  if (!errors.length) {
    const m = bodyText.match(rejectRe);
    if (m) errors.push(`page says: ${m[0]}`);
  }

  return {
    confirmed: Boolean(hit) && !(submitBtn && errors.length),
    evidence: hit || '',
    form_present: Boolean(submitBtn),
    errors,
  };
}
"""


# After clicking submit, some boards (Oura's Greenhouse) show an email
# verification step: a code was emailed and must be typed in to finish. Detect
# that state and tag the code input(s) so we can fill them. Split single-char
# code boxes are captured as a group.
_VERIFY_PROMPT_JS = r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden'
        || Number(style.opacity || 1) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4;
  };
  const bodyText = (document.body.innerText || '').toLowerCase();
  const asksCode = new RegExp(
    '(verification|confirmation|one[\\s-]?time|security)\\s*code'
    + '|enter the code|enter your code|code we (?:sent|emailed)'
    + '|we (?:just )?(?:sent|emailed) (?:you )?a (?:code|verification)'
    + '|verify your email|confirm your email|check your (?:email|inbox)'
  ).test(bodyText);
  if (!asksCode) return { present: false };
  const inputs = Array.from(document.querySelectorAll('input')).filter(el => {
    const t = (el.type || 'text').toLowerCase();
    if (['hidden', 'checkbox', 'radio', 'file', 'submit', 'button', 'email',
         'password'].includes(t)) return false;
    if (!visible(el)) return false;
    const meta = `${el.name || ''} ${el.id || ''} ${el.className || ''} `
      + `${el.getAttribute('autocomplete') || ''} `
      + `${el.getAttribute('inputmode') || ''} `
      + `${el.getAttribute('aria-label') || ''} ${el.placeholder || ''}`;
    if (/code|otp|verif|token|\bpin\b/i.test(meta)) return true;
    if ((el.getAttribute('autocomplete') || '').toLowerCase() === 'one-time-code')
      return true;
    if ((el.getAttribute('inputmode') || '').toLowerCase() === 'numeric')
      return true;
    const ml = parseInt(el.getAttribute('maxlength') || '0', 10);
    if (ml >= 1 && ml <= 8) return true;
    return false;
  });
  if (!inputs.length) return { present: true, field: false };
  inputs.forEach((el, i) => el.setAttribute('data-ja-verify', i));
  const m = bodyText.match(/[^.\n]*\bcode\b[^.\n]*/);
  return { present: true, field: true, count: inputs.length,
           label: (m ? m[0] : 'email verification').trim().slice(0, 140) };
}
"""


def _click_verify_button(page) -> str:
    """Click the verify/confirm/continue button on an email-code step. Returns
    the button text clicked, or '' if none found (the code field's Enter key
    may still submit)."""
    return page.evaluate(r"""
      () => {
        const visible = (el) => {
          const s = window.getComputedStyle(el);
          if (s.display === 'none' || s.visibility === 'hidden'
              || Number(s.opacity || 1) === 0) return false;
          const r = el.getBoundingClientRect();
          return r.width > 4 && r.height > 4;
        };
        const score = (raw) => {
          const t = (raw || '').trim().toLowerCase();
          if (!t) return 0;
          if (/^(verify|confirm)( ?code| email)?$/.test(t)) return 100;
          if (/^(submit|continue|next|done)$/.test(t)) return 80;
          if (/verify|confirm/.test(t)) return 60;
          if (/submit|continue/.test(t)) return 40;
          return 0;
        };
        const btns = Array.from(document.querySelectorAll(
          'button, input[type=submit]')).filter(visible);
        let best = null, bestScore = 0;
        for (const b of btns) {
          const s = score(b.innerText || b.value || '');
          if (s > bestScore) { best = b; bestScore = s; }
        }
        if (best) { best.scrollIntoView({block:'center'}); best.click();
          return (best.innerText || best.value || '').trim(); }
        return '';
      }
    """)


def _complete_email_verification(page, cfg: Config, job: Job,
                                 submit_epoch: float, prompt: dict) -> str:
    """Finish an email verification-code step. Returns 'entered' when a code
    read from the inbox was submitted, otherwise a human-readable hold note
    (prefixed '[verification]') explaining why the human must finish it.
    Never types a guessed code — every uncertainty holds the job.

    Enforces the three trust gates: gate 1 (an empty sender allowlist for this
    ATS holds and asks for a real sample), gates 1+2 inside fetch, and gate 3
    (code_usable_for) binds the code to this ATS + time window before entry."""
    inbox = cfg.email_verify.imap_user or cfg.profile.email or "your application email"
    if not prompt.get("field"):
        logger.info("verification prompt detected but no code field found: %r",
                    prompt.get("label"))
        return ("[verification] a code step appeared but no code field was "
                f"found — enter the code sent to {inbox} manually.")
    if not getattr(cfg.email_verify, "enabled", False):
        return (f"[verification] email_verify is off — enter the code sent to "
                f"{inbox} in the open form to finish.")

    ats = job.source
    allowlist = (cfg.verification.senders or {}).get(ats)
    if not allowlist:
        # Gate 1, empty allowlist: do not guess a sender for a new ATS.
        logger.info("verification held: no sender allowlist for ats=%r", ats)
        return (f"[verification] no sender allowlist configured for {ats}; add "
                f"the real sender domain from the received email to "
                f"verification.senders in config, then re-run. (Code held, "
                f"not guessed.)")

    from .email_verify import code_usable_for, fetch_verification_code
    vc = fetch_verification_code(cfg, ats, since_epoch=submit_epoch)
    if vc is None:
        return (f"[verification] no code from an allowlisted {ats} sender "
                f"arrived in time — enter the code sent to {inbox} manually.")
    # Gate 3: bind the code to this form's ATS and the poll window.
    if not code_usable_for(vc, ats, submit_epoch, time.time(),
                           cfg.verification.clock_skew_seconds):
        logger.info("verification held: code failed ATS/time binding "
                    "(code ats=%s, form ats=%s)", vc.ats, ats)
        return (f"[verification] a code was found but failed ATS/time binding "
                f"— held. Enter the code sent to {inbox} manually.")

    code = vc.code
    count = int(prompt.get("count") or 1)
    try:
        if count > 1 and len(code) == count:
            for i, ch in enumerate(code):
                page.fill(f'[data-ja-verify="{i}"]', ch)
        else:
            page.fill('[data-ja-verify="0"]', code)
    except Exception as exc:
        logger.warning("failed to enter verification code: %s", exc)
        return (f"[verification] could not type the code into the form — "
                f"enter the code sent to {inbox} manually.")
    _click_verify_button(page)
    logger.info("submitted email verification code for ats=%s (%d chars)",
                ats, len(code))
    return "entered"


def _safe_screenshot(page, path: str) -> str:
    """Screenshot without ever throwing. A capture failure (fonts still
    loading, page too tall, 30s timeout) must NEVER destroy a computed
    submit verdict — that was the exact ElevenLabs failure where a timed-out
    screenshot turned a real outcome into 'error: state unknown'. Tries a
    full-page shot, falls back to viewport-only, then gives up quietly."""
    for full_page in (True, False):
        try:
            page.screenshot(path=path, full_page=full_page,
                            animations="disabled", timeout=12000)
            return path
        except Exception:
            continue
    return ""


def _system_timezone_id() -> str:
    """Best-effort IANA name of the machine's timezone (e.g.
    'America/Los_Angeles') so the browser context reports the truth instead of
    headless-Chromium's UTC. Reads the /etc/localtime symlink used on macOS and
    most Linux. Returns '' if it cannot be determined — the caller then leaves
    the timezone unset and Playwright inherits the OS default."""
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return ""
    marker = "zoneinfo/"
    if marker not in target:
        return ""
    cand = target.split(marker, 1)[1].strip()
    # Sanity filter: a real zone id is like "Area/City", no spaces, short.
    if "/" in cand and " " not in cand and 0 < len(cand) < 64:
        return cand
    return ""


def _context_kwargs(cfg: Config) -> dict:
    """Locale + timezone for the browser context. Honest values (see the Apply
    config): they make a real applicant from Seattle look like one instead of a
    UTC headless bot, without spoofing anything."""
    kwargs: dict = {}
    if cfg.apply.locale:
        kwargs["locale"] = cfg.apply.locale
    tz = cfg.apply.timezone_id or _system_timezone_id()
    if tz:
        kwargs["timezone_id"] = tz
    return kwargs


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def _norm_url(u: str) -> str:
    """Strip fragment/query so a benign ?utm= change is not read as a
    navigation, but a real redirect to /confirmation still is."""
    return re.split(r"[?#]", (u or "").rstrip("/"), 1)[0].lower()


def _work_meta(f: dict) -> str:
    return " ".join(
        str(f.get(k) or "") for k in ("name", "id", "placeholder", "context")
    )


def _work_field_meta(f: dict) -> str:
    return " ".join(
        str(f.get(k) or "") for k in ("name", "id", "placeholder")
    )


def _work_index_for_field(f: dict, key: str | None,
                          counts: dict[str, int]) -> int:
    code_meta = " ".join(str(f.get(k) or "") for k in ("name", "id")).lower()
    m = re.search(r"(?:experience|employment|work|job|position)[^\d]*(\d+)", code_meta)
    if m:
        return int(m.group(1)) + 1

    text = _norm(f"{f.get('label', '')} {_work_meta(f)}")
    for pattern in (
        r"(?:experience|employment|work|job)[^\d]{0,12}(\d+)",
        r"(\d+)[^\d]{0,12}(?:experience|employment|work|job)",
    ):
        m = re.search(pattern, text)
        if m:
            return max(int(m.group(1)), 1)
    if key:
        counts[key] = counts.get(key, 0) + 1
        return counts[key]
    return 1


def _is_false_answer(value: str) -> bool:
    return _norm(value) in {"no", "n", "false", "not current", "not currently"}


def _is_true_answer(value: str) -> bool:
    nval = _norm(value)
    return nval in {"yes", "y", "true", "current", "currently"} or nval.startswith("yes")


def _check_radio_option(page, sel: str, name: str, value: str) -> bool:
    return bool(page.evaluate(r"""
      ({sel, name, value}) => {
        const norm = (s) => (s || '').toLowerCase()
          .replace(/[^a-z0-9 ]/g, ' ')
          .replace(/\s+/g, ' ').trim();
        const current = document.querySelector(sel);
        const group = name
          ? Array.from(document.querySelectorAll(
              `input[type="radio"][name="${CSS.escape(name)}"]`))
          : (current ? [current] : []);
        const textsFor = (el) => {
          const texts = [el.value || '', el.getAttribute('aria-label') || ''];
          if (el.id) {
            const l = document.querySelector(
              `label[for="${CSS.escape(el.id)}"]`);
            if (l) texts.push(l.innerText || '');
          }
          const wrap = el.closest('label');
          if (wrap) texts.push(wrap.innerText || '');
          return texts.map(norm).filter(Boolean);
        };
        const want = norm(value);
        for (const el of group) {
          if (textsFor(el).some(t => t === want)) {
            el.click();
            return true;
          }
        }
        return false;
      }
    """, {"sel": sel, "name": name, "value": value}))


def _expand_work_experience_sections(page, book: AnswerBook) -> None:
    count = book.work_experience_count()
    if count <= 1:
        return
    page.evaluate(r"""
      (targetCount) => {
        const visible = (el) => {
          const style = window.getComputedStyle(el);
          if (style.display === 'none' || style.visibility === 'hidden'
              || Number(style.opacity || 1) === 0) return false;
          const r = el.getBoundingClientRect();
          return r.width > 4 && r.height > 4;
        };
        const labelFor = (el) => {
          if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
          if (el.id) {
            const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (l) return l.innerText;
          }
          const p = el.closest('label');
          if (p) return p.innerText;
          const node = el.closest('fieldset, section, [data-field-path], div');
          const lab = node && node.querySelector('label, legend');
          return lab ? lab.innerText : '';
        };
        const contextFor = (el) => {
          const node = el.closest('fieldset, section, [data-field-path], div');
          return node ? (node.innerText || '').replace(/\s+/g, ' ').trim() : '';
        };
        const controls = Array.from(document.querySelectorAll('input, textarea, select'))
          .filter(visible);
        const existingCompanies = controls.filter(el => {
          const text = `${labelFor(el)} ${el.name || ''} ${el.id || ''} ${el.placeholder || ''} ${contextFor(el)}`.toLowerCase();
          const companyLike = /employer|company name|\bcompany\b/.test(text);
          const workLike = /work experience|employment history|work history|professional experience|employer|job title/.test(text);
          return companyLike && workLike;
        }).length;
        const neededClicks = Math.max(targetCount - Math.max(existingCompanies, 1), 0);
        const addPattern = /add (another )?(work |professional |employment )?(experience|employment|job|position)|add position|add job/i;
        for (let i = 0; i < neededClicks; i++) {
          const btn = Array.from(document.querySelectorAll('button, a')).find(
            b => visible(b) && addPattern.test((b.innerText || b.textContent || '').trim())
          );
          if (!btn) break;
          btn.click();
        }
      }
    """, count)
    page.wait_for_timeout(500)


def fill_and_submit(job: Job, cfg: Config, book: AnswerBook,
                    live: bool, cover_letter_path: str = "",
                    attended_handoff=None) -> SubmitResult:
    """Fill (and, when live, submit) one job's application form.

    attended_handoff: OPTIONAL callback used only by the diagnostic probe
    (jobagent probe-ashby). When set, it is invoked ONLY on the dry-run path
    (live=False) with the open page after fill + validation — so a human can
    inspect and optionally submit themselves. It is NEVER invoked on the live
    path, and because live=False returns before the submit-click block below,
    setting it guarantees no automated submit. The live callers pass None.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return SubmitResult(
            "needs_review",
            note="playwright not installed. run: pip install playwright && "
                 "playwright install chromium",
        )

    shot_dir = Path(cfg.apply.screenshot_dir)
    shot_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() else "-" for c in f"{job.company}-{job.title}")[:60]
    shot_path = str(shot_dir / f"{safe}.png")

    filled: list[str] = []
    unanswered: list[str] = []
    # Correctness failures (a sensitive field filled WRONG, a post-fill
    # verification mismatch): these hold UNCONDITIONALLY at the submit boundary,
    # unlike `unanswered` no-answers which the hold_on_unknown_required flag
    # gates. The invariant is "never submit something wrong", not only "never
    # submit something empty".
    strict_holds: list[str] = []
    cover_pdf_path = ""

    if cover_letter_path:
        try:
            cover_pdf_path = ensure_cover_letter_pdf(job, cfg, cover_letter_path)
            cover_errors = validate_cover_letter_package(
                job, cfg, cover_letter_path, cover_pdf_path
            )
        except Exception as exc:
            cover_errors = [f"cover letter validation failed: {exc}"]
        if cover_errors:
            return SubmitResult(
                "needs_review",
                note="cover letter package failed validation; not opening "
                     "browser or submitting",
                unanswered_required=[
                    f"cover letter package: {err}" for err in cover_errors
                ],
            )

    def pause_for_review(page) -> None:
        if not live and not cfg.apply.headless and cfg.apply.dry_run_pause_seconds > 0:
            page.wait_for_timeout(cfg.apply.dry_run_pause_seconds * 1000)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=cfg.apply.headless)
        try:
            context = browser.new_context(**_context_kwargs(cfg))
        except Exception:
            # A bad timezone id must never break submitting; fall back to a
            # plain context that inherits the OS defaults.
            context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(job.apply_url or job.url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)

            if page.evaluate(_CAPTCHA_JS):
                _safe_screenshot(page, shot_path)
                pause_for_review(page)
                browser.close()
                raise CaptchaEncountered()

            _expand_work_experience_sections(page, book)
            work_counts: dict[str, int] = {}
            processed: set[tuple[str, str, str, str, str]] = set()
            expected_values: dict[tuple[str, str, str, str, str], dict[str, str | bool]] = {}
            cover_text = _read_cover_letter(cover_letter_path)

            # Some ATSes reveal follow-up fields after a select is answered
            # (Greenhouse EEO race after Hispanic/Latino, for example). Make
            # a few passes so those newly visible fields are filled too.
            # prev_scan is the previous pass's field signatures; comparing it
            # to each new scan surfaces the DOM re-render signal — a field that
            # was present and then vanished (or appeared) is exactly suspect (b)
            # for the State-after-City bug. Limitation: unnamed repeated fields
            # (work-experience rows) share a signature and can log spurious
            # appear/disappear churn; that is a known false signal, not fixed
            # here.
            prev_scan: set[tuple[str, str, str, str, str]] = set()
            for _pass in range(3):
                fields = page.evaluate(_EXTRACT_JS)
                progress = False

                cur_scan = {_field_signature(f) for f in fields}
                if _pass:
                    for sig in cur_scan - prev_scan:
                        logger.info("scan-diff pass %d: field APPEARED %r [%s]",
                                    _pass, sig[3] or sig[1], sig[0])
                    for sig in prev_scan - cur_scan:
                        logger.info("scan-diff pass %d: field DISAPPEARED %r [%s]",
                                    _pass, sig[3] or sig[1], sig[0])
                prev_scan = cur_scan

                for f in fields:
                    sig = _field_signature(f)
                    if sig in processed:
                        continue
                    sel = f'[data-ja-idx="{f["idx"]}"]'
                    label = f["label"] or f["name"]
                    kind = f["kind"]
                    context = str(f.get("context") or "")

                    # Password-ish fields are never auto-filled ("Portfolio
                    # site + Password" was getting a bare URL). Required ones
                    # hold the job for a human decision.
                    evidence = f.get("evidence") or "none"
                    if _never_fill(label, cfg):
                        if f["required"]:
                            unanswered.append(
                                f"{label} (never auto-filled; answer yourself)")
                        logger.info(
                            "field %r [%s] required=%s via=%s -> skipped "
                            "(never-fill)", label, kind, f["required"], evidence)
                        processed.add(sig)
                        continue

                    # Resume upload: use the profile resume for any file input.
                    if kind == "file":
                        label_low = label.lower()
                        is_cover = "cover" in label_low
                        is_resume = any(w in label_low for w in ("resume", "cv"))
                        path = ""
                        if is_cover:
                            path = cover_pdf_path
                        elif is_resume or f["required"]:
                            # Role-specific resume, chosen by job title
                            # (product designer / ux designer / design
                            # engineer variants).
                            path = cfg.profile.resume_for_title(job.title)

                        if path and Path(path).exists():
                            page.set_input_files(sel, path)
                            filled.append(f"{label or Path(path).name} (file: {path})")
                            logger.info(
                                "field %r [file] required=%s via=%s -> attached %s",
                                label, f["required"], evidence, path)
                            progress = True
                        elif f["required"]:
                            unanswered.append(label or "file upload")
                            logger.info(
                                "field %r [file] required=%s via=%s -> no-answer "
                                "(held)", label, f["required"], evidence)
                        else:
                            logger.info(
                                "field %r [file] required=%s via=%s -> skipped "
                                "(optional, no file)", label, f["required"], evidence)
                        processed.add(sig)
                        continue

                    resolve_kind = "select" if kind in ("yesno", "combobox") else kind
                    work_key = book._work_field_key(
                        label, _work_field_meta(f), f.get("options") or None
                    )
                    work_index = _work_index_for_field(f, work_key, work_counts)
                    ans = book.resolve_work_experience(
                        label,
                        resolve_kind,
                        f.get("options") or None,
                        index=work_index,
                        meta=_work_field_meta(f),
                        context=context,
                    )
                    if ans is None:
                        ans = book.resolve(label, resolve_kind, f.get("options") or None)
                    if ans is None and cover_text and _is_cover_letter_text_field(
                        label, context, kind
                    ):
                        ans = Resolved(cover_text, "cover_letter", 0.95)
                    if ans is None:
                        if f["required"]:
                            unanswered.append(
                                label or f["name"] or "(unlabeled required field)")
                        logger.info(
                            "field %r [%s] required=%s via=%s -> no-answer (%s)",
                            label, kind, f["required"], evidence,
                            "held" if f["required"] else "left blank")
                        processed.add(sig)
                        continue

                    try:
                        if kind == "select":
                            page.select_option(sel, label=ans.value)
                        elif kind == "yesno":
                            clicked = page.evaluate(r"""
                              ({idx, name, value}) => {
                                const byIdx = `[data-ja-idx="${idx}"]`;
                                const byName = name
                                  ? `input[name="${CSS.escape(name)}"]`
                                  : '';
                                const el = document.querySelector(byIdx)
                                  || (byName ? document.querySelector(byName) : null);
                                const root = el && el.parentElement;
                                if (!root) return false;
                                const target = Array.from(
                                  root.querySelectorAll('button')).find(
                                    b => (b.innerText || '').trim().toLowerCase()
                                      === value.trim().toLowerCase());
                                if (!target) return false;
                                target.click();
                                return true;
                              }
                            """, {
                                "idx": f["idx"],
                                "name": f.get("name") or "",
                                "value": ans.value,
                            })
                            if not clicked:
                                raise RuntimeError("yes/no option not found")
                        elif kind == "combobox":
                            strict = _is_sensitive_choice(label)
                            picked = _fill_combobox(page, sel, ans.value, strict=strict)
                            filled.append(
                                f"{label} = {ans.value} -> selected {picked!r}  "
                                f"[{ans.source}]")
                            if strict:
                                expected_values[sig] = {
                                    "label": label,
                                    "value": ans.value,
                                    "strict": True,
                                }
                            logger.info(
                                "field %r [combobox] required=%s via=%s -> "
                                "selected %r [%s]", label, f["required"],
                                evidence, picked, ans.source)
                            processed.add(sig)
                            progress = True
                            continue
                        elif kind == "checkbox":
                            if _is_false_answer(ans.value):
                                page.uncheck(sel)
                            elif _is_true_answer(ans.value):
                                page.check(sel)
                            else:
                                page.check(sel)
                        elif kind == "radio":
                            clicked = _check_radio_option(
                                page, sel, f.get("name") or "", ans.value)
                            if not clicked:
                                raise RuntimeError(
                                    f"radio option not found for {ans.value!r}")
                        else:
                            page.fill(sel, ans.value)
                            # React apps occasionally swallow programmatic fills;
                            # verify the value registered, else type it for real.
                            try:
                                if page.locator(sel).first.input_value() != ans.value:
                                    page.locator(sel).first.fill("")
                                    page.locator(sel).first.press_sequentially(
                                        ans.value, delay=15)
                                # Blur so the ATS's own validation runs now and
                                # stale "field is required" errors from before
                                # the fill clear instead of tripping the
                                # pre-submit sweep.
                                page.locator(sel).first.evaluate(
                                    "el => { el.dispatchEvent(new Event('change', {bubbles: true})); el.blur(); }")
                            except Exception:
                                pass
                        filled.append(f"{label} = {ans.value}  [{ans.source}]")
                        logger.info(
                            "field %r [%s] required=%s via=%s -> filled from %s: %r",
                            label, kind, f["required"], evidence, ans.source,
                            ans.value[:60])
                        if kind not in ("select", "yesno", "radio", "checkbox") \
                                and logger.isEnabledFor(logging.DEBUG):
                            try:
                                readback = page.locator(sel).first.input_value()
                                logger.debug("field %r readback=%r", label, readback)
                            except Exception:
                                pass
                        if _is_sensitive_choice(label):
                            expected_values[sig] = {
                                "label": label,
                                "value": ans.value,
                                "strict": True,
                            }
                        if ans.source == "cover_letter":
                            expected_values[sig] = {
                                "label": label,
                                "value": ans.value[:160],
                                "strict": False,
                            }
                        processed.add(sig)
                        progress = True
                    except Exception as exc:
                        if f["required"] or _is_sensitive_choice(label):
                            tag = f"{label} (fill failed: {exc})"
                            unanswered.append(tag)
                            # A sensitive field we could not set may be left on a
                            # WRONG prior value, not just empty — hold it
                            # unconditionally, not only under the flag.
                            if _is_sensitive_choice(label):
                                strict_holds.append(tag)
                        logger.info(
                            "field %r [%s] required=%s via=%s -> fill FAILED: %s",
                            label, kind, f["required"], evidence, exc)
                        processed.add(sig)

                page.wait_for_timeout(700)
                if not progress:
                    break

            # Post-fill sweep: catch required fields that LOOK filled in our
            # log but did not register on the page (the exact failure mode of
            # the 2026-07-01 Greenhouse batch). Uses the SAME gather+verdict as
            # the submit-boundary invariant below (one rule set). Fails OPEN
            # here — a flaky evaluate must not mask a hold; the boundary check
            # (which fails closed) is the authoritative gate.
            try:
                page.wait_for_timeout(400)
                sweep_fail = _gather_presubmit_failures(page)
            except Exception as exc:
                logger.warning("post-fill sweep failed: %s", exc)
                sweep_fail = []
            # Skip sweep entries whose field is already held under a
            # near-identical label ("State*" from the fill loop vs "State"
            # from the sweep) — one hold line per field, not two.
            held_labels = {
                _norm(re.split(r"[\(\[]", u, 1)[0].rstrip("* "))
                for u in unanswered
            }
            for miss in sweep_fail:
                logger.info(
                    "post-fill sweep: %r [%s] still unfilled (required via %s): %s",
                    miss["label"], miss["kind"], miss["evidence"], miss["reason"])
                if _norm(str(miss["label"]).rstrip("* ")) in held_labels:
                    continue
                tag = _format_presubmit_fail(miss, suffix="did not register on page")
                if tag not in unanswered:
                    unanswered.append(tag)

            if expected_values:
                try:
                    latest_fields = page.evaluate(_EXTRACT_JS)
                    checks = []
                    for f in latest_fields:
                        sig = _field_signature(f)
                        expected = expected_values.get(sig)
                        if not expected:
                            continue
                        checks.append({
                            "idx": f["idx"],
                            "kind": f["kind"],
                            "label": str(expected["label"]),
                            "value": str(expected["value"]),
                            "strict": bool(expected["strict"]),
                        })
                    for check in page.evaluate(_VERIFY_FILLED_JS, checks):
                        expected = str(check.get("value") or "")
                        actual = str(check.get("actual") or "")
                        strict = bool(check.get("strict"))
                        if not _value_matches(expected, actual, strict=strict):
                            tag = (f"{check.get('label') or 'field'} "
                                   f"(verification mismatch: expected "
                                   f"{expected!r}, saw {actual!r})")
                            unanswered.append(tag)
                            strict_holds.append(tag)
                except Exception as exc:
                    tag = f"post-fill verification failed: {exc}"
                    unanswered.append(tag)
                    strict_holds.append(tag)

            _safe_screenshot(page, shot_path)

            # Hold rather than submit a guessed or half-filled application.
            if unanswered and cfg.apply.hold_on_unknown_required:
                pause_for_review(page)
                browser.close()
                return SubmitResult(
                    "needs_review",
                    note=f"filled {len(filled)} fields; held because required fields "
                         f"were unanswered: {', '.join(unanswered[:8])}",
                    screenshot=shot_path, filled=filled, unanswered_required=unanswered,
                )

            # PRE-SUBMIT VALIDATION — the safety invariant. A FRESH DOM gather
            # taken right at the submit boundary; earlier field lists/handles
            # are deliberately NOT reused, because an autocomplete re-render can
            # drop a required field (Greenhouse's State after City) from the DOM
            # that an earlier scan saw. This holds UNCONDITIONALLY — even when
            # hold_on_unknown_required is False, which previously allowed a
            # submit with unanswered fields; this invariant does not. It never
            # fills or corrects anything: its only outputs are proceed or hold.
            try:
                presubmit_fail = _gather_presubmit_failures(page)
            except Exception as exc:
                # Fail CLOSED: if we cannot read the form's state we cannot
                # prove it is complete, so we hold rather than risk a bad
                # submit ("hold on unknown").
                logger.warning("presubmit gather failed, holding: %s", exc)
                presubmit_fail = [{
                    "label": "(presubmit gather failed)", "kind": "",
                    "required": True, "evidence": "error", "reason": str(exc),
                }]
            for miss in presubmit_fail:
                logger.info(
                    "presubmit: %r [%s] required via %s unfilled at submit: %s",
                    miss["label"], miss["kind"], miss["evidence"], miss["reason"])
            presubmit_notes = [_format_presubmit_fail(m) for m in presubmit_fail]
            # Build the unconditional hold set from three sources, all of which
            # block submission regardless of hold_on_unknown_required:
            #   - presubmit_fail: required fields empty/placeholder on the fresh
            #     DOM (the invariant proper)
            #   - strict_holds: fields filled WRONG (verification mismatch,
            #     sensitive fill failure)
            #   - unanswered: required fields the filler could not answer. When
            #     the flag is True we already held above; when it is False we
            #     reach here and STILL must not submit an unanswered required
            #     field. This also closes the residual where a non-standard
            #     placeholder <select> is structurally indistinguishable from a
            #     real selection by DOM alone — we know it is unfilled because we
            #     never resolved an answer for it.
            def _hold_key(note: str) -> str:
                return note.split(" [", 1)[0].split(" (", 1)[0].strip().lower()

            hold_notes: list[str] = []
            seen_holds: set[str] = set()
            for note in [*presubmit_notes, *strict_holds, *unanswered]:
                key = _hold_key(note)
                if key in seen_holds:
                    continue
                seen_holds.add(key)
                hold_notes.append(note)
            hold_summary = "; ".join(hold_notes[:8])
            should_hold = bool(hold_notes)

            if not live:
                # Attended diagnostic probe: hand the OPEN, filled page to the
                # caller (a human), then close. The submit-click block below is
                # never reached here — live is False, so we return in this
                # branch. This is the no-auto-submit guarantee in code.
                if attended_handoff is not None:
                    try:
                        attended_handoff(page, {
                            "filled": list(filled),
                            "presubmit_fail": list(presubmit_fail),
                            "presubmit_notes": list(presubmit_notes),
                            "hold_notes": list(hold_notes),
                            "should_hold": should_hold,
                            "shot_path": shot_path,
                        })
                    finally:
                        browser.close()
                    return SubmitResult(
                        "needs_review",
                        note="[probe] attended hand-off complete — bot never "
                             "auto-submitted",
                        screenshot=shot_path, filled=filled,
                        unanswered_required=hold_notes,
                    )
                # Dry-run parity: a dry run must predict what a live run would
                # do, so run the same validation and report the verdict.
                pause_for_review(page)
                browser.close()
                if should_hold:
                    return SubmitResult(
                        "needs_review",
                        note=f"[presubmit] would hold: {len(hold_notes)} "
                             f"required field(s) empty or mismatched at submit "
                             f"(live_submit off): {hold_summary}",
                        screenshot=shot_path, filled=filled,
                        unanswered_required=hold_notes,
                    )
                return SubmitResult(
                    "needs_review",
                    note=f"dry run: filled {len(filled)} fields; would submit "
                         f"— presubmit validation passed (live_submit off). "
                         f"Review the screenshot.",
                    screenshot=shot_path, filled=filled,
                )

            if should_hold:
                _safe_screenshot(page, shot_path)
                browser.close()
                return SubmitResult(
                    "needs_review",
                    note=f"[presubmit] held: {len(hold_notes)} required "
                         f"field(s) empty or mismatched at submit — NOT "
                         f"submitted: {hold_summary}",
                    screenshot=shot_path, filled=filled,
                    unanswered_required=hold_notes,
                )

            # Live submit. Score candidates instead of clicking the first
            # /submit|apply|send/ match: on Figma's Greenhouse board that
            # naive regex hit "Quick Apply with MyGreenhouse" (a login
            # shortcut ABOVE the form) so every submission silently became
            # a no-op. Prefer the real submit control and exclude
            # third-party quick-apply / autofill / alert buttons entirely.
            pre_url = page.url
            submit_epoch = time.time()   # for the email-verification code window
            clicked = page.evaluate(r"""
              () => {
                const visible = (el) => {
                  const style = window.getComputedStyle(el);
                  if (style.display === 'none' || style.visibility === 'hidden'
                      || Number(style.opacity || 1) === 0) return false;
                  const r = el.getBoundingClientRect();
                  return r.width > 4 && r.height > 4;
                };
                const bad = /quick apply|mygreenhouse|linkedin|indeed|autofill|auto-fill|create( job)? alert|job alert|sign in|log ?in|locate me|dropbox|google drive|cancel/i;
                const score = (raw) => {
                  const t = (raw || '').trim().toLowerCase().replace(/\s+/g, ' ');
                  if (!t || bad.test(t)) return 0;
                  if (/^submit( application| your application)?$/.test(t)) return 100;
                  if (/^send( my)? application$/.test(t)) return 90;
                  if (/^apply( now| for this (job|position|role))?$/.test(t)) return 80;
                  if (/submit/.test(t)) return 60;
                  if (/\bapply\b/.test(t)) return 30;
                  return 0;
                };
                const btns = Array.from(document.querySelectorAll(
                  'button, input[type=submit]')).filter(visible);
                let best = null, bestScore = 0;
                for (const b of btns) {
                  const s = score(b.innerText || b.value || '');
                  if (s > bestScore) { best = b; bestScore = s; }
                }
                if (best) {
                  best.scrollIntoView({block: 'center'});
                  best.click();
                  return (best.innerText || best.value || '').trim();
                }
                return '';
              }
            """)
            page.wait_for_timeout(3000)
            if not clicked:
                _safe_screenshot(page, shot_path)
                browser.close()
                return SubmitResult(
                    "needs_review",
                    note="could not find a submit button; finish this one by hand",
                    screenshot=shot_path, filled=filled,
                )

            # Email verification step (Oura's Greenhouse): a code was emailed
            # and must be entered to complete the application. Complete it from
            # the applicant's own inbox when configured; otherwise hold and tell
            # the human to enter the code — we never type a guessed code.
            try:
                prompt = page.evaluate(_VERIFY_PROMPT_JS)
            except Exception:
                prompt = {"present": False}
            if prompt.get("present"):
                outcome = _complete_email_verification(
                    page, cfg, job, submit_epoch, prompt)
                if outcome == "entered":
                    page.wait_for_timeout(3500)  # let confirmation swap in
                else:
                    # outcome is a '[verification] ...' hold note explaining
                    # exactly why the human must finish (gate 1 uncalibrated,
                    # no code, binding failed, feature off, ...).
                    _safe_screenshot(page, shot_path)
                    browser.close()
                    return SubmitResult(
                        "needs_review",
                        note=f"submitted, but this board requires an emailed "
                             f"verification code to finish. {outcome}",
                        screenshot=shot_path, filled=filled,
                        unanswered_required=[outcome],
                    )

            # VERIFY the outcome instead of trusting the click. Three honest
            # outcomes, never just "clicked so probably fine":
            #   applied              -> the page confirmed it (phrase or a
            #                           thank-you/confirmation redirect)
            #   needs_review         -> the form is still on screen, usually
            #                           with validation errors: NOT submitted
            #   submitted_unverified -> the form closed with no error and no
            #                           confirmation text we recognize: most
            #                           likely submitted, but we cannot prove
            #                           it in-page. Verify by email.
            verdict = page.evaluate(_SUBMIT_VERIFY_JS)
            # ATSes often need a beat to swap in the confirmation view.
            if not verdict.get("confirmed") and verdict.get("form_present"):
                page.wait_for_timeout(4000)
                verdict = page.evaluate(_SUBMIT_VERIFY_JS)
            post_url = page.url
            _safe_screenshot(page, shot_path)
            browser.close()

            errors = verdict.get("errors") or []
            form_present = bool(verdict.get("form_present"))
            url_changed = _norm_url(post_url) != _norm_url(pre_url)
            url_confirms = bool(re.search(
                r"thank|confirm|submitted|success|complete|received|applied",
                (post_url or ""), re.I))

            if verdict.get("confirmed") or (url_confirms and not (form_present and errors)):
                evidence = verdict.get("evidence", "") or f"redirected to {post_url}"
                return SubmitResult(
                    "applied",
                    note=f"submitted with {len(filled)} fields filled via "
                         f"button {clicked!r}; confirmation: {evidence!r} | "
                         f"final url: {post_url}",
                    screenshot=shot_path, filled=filled,
                )

            if form_present and errors:
                detail = "; ".join(errors[:6])
                return SubmitResult(
                    "needs_review",
                    note=f"clicked submit but the page REJECTED it — NOT "
                         f"submitted. {detail}. Fix these and re-run.",
                    screenshot=shot_path, filled=filled,
                    unanswered_required=[f"submit blocked: {e}" for e in errors[:8]],
                )

            if not form_present:
                # The form is gone and nothing complained, but we did not see
                # a confirmation we recognize. Treat as probably-submitted and
                # say so plainly instead of pretending certainty either way.
                where = f" and moved to {post_url}" if url_changed else ""
                return SubmitResult(
                    "submitted_unverified",
                    note=f"submit likely went through: the form closed{where} "
                         f"with no error, but the page showed no confirmation "
                         f"text we recognize. Verify by email; the screenshot "
                         f"shows the final page.",
                    screenshot=shot_path, filled=filled,
                )

            # Form still on screen, no harvested errors, no confirmation: we
            # cannot claim it submitted. Hold it.
            return SubmitResult(
                "needs_review",
                note=f"clicked {clicked!r} but the form is still on screen "
                     f"with no confirmation and no visible error — NOT "
                     f"submitted. Finish this one by hand.",
                screenshot=shot_path, filled=filled,
            )

        except CaptchaEncountered:
            raise
        except Exception as exc:
            try:
                browser.close()
            except Exception:
                pass
            return SubmitResult("error", note=f"submit failed: {exc}", screenshot=shot_path)
