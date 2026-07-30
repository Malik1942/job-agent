"""Config-layer tests for profile links + cover-letter families.

Run from the repo root:
    python3 scripts/test_config_coverletter.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobagent.config import load_config, profile_gaps  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"[{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


MINIMAL = """
profile:
  name: "Alex Rivera"
  email: "alex.rivera@testmail.local"
  phone: "+1 555 010 0100"
  website: "https://alexrivera.example.com"
  linkedin: "https://www.linkedin.com/in/alex-rivera-example"
  titles: ["Product Designer"]
  resume_path: "resume.pdf"
  answers_path: "answers.md"
coverletter:
  default_family: product_designer
  families:
    product_designer:
      keywords: []
      identity: "I am a product designer."
      craft: "I prototype in code."
      closing_mix: "craft and judgment"
      marker: "product designer"
sources:
  - { kind: greenhouse, token: "example" }
"""


def load_from_text(text: str):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(text)
        f.flush()
        return load_config(f.name)


cfg = load_from_text(MINIMAL)
check("profile.website parsed", cfg.profile.website == "https://alexrivera.example.com")
check("example email is a gap (sample config must not pass the gate)",
      any("email" in g for g in profile_gaps(
          load_from_text(MINIMAL.replace("alex.rivera@testmail.local",
                                         "alex.rivera@example.com")))))
check("profile.linkedin parsed", cfg.profile.linkedin.endswith("alex-rivera-example"))
check("coverletter default_family", cfg.coverletter.default_family == "product_designer")
check("family parsed as dataclass",
      cfg.coverletter.families["product_designer"].marker == "product designer")
check("projects default empty", cfg.coverletter.projects == [])
check("ready profile has no gaps", profile_gaps(cfg) == [])

# Gaps: strip essentials one at a time.
cfg2 = load_from_text(MINIMAL.replace('name: "Alex Rivera"', 'name: ""'))
check("missing name reported", any("profile.name" in g for g in profile_gaps(cfg2)))
cfg3 = load_from_text(MINIMAL.replace("""coverletter:
  default_family: product_designer
  families:
    product_designer:
      keywords: []
      identity: "I am a product designer."
      craft: "I prototype in code."
      closing_mix: "craft and judgment"
      marker: "product designer"
""", ""))
check("missing families reported",
      any("coverletter.families" in g for g in profile_gaps(cfg3)))
cfg4 = load_from_text(MINIMAL.replace('identity: "I am a product designer."',
                                      'identity: ""'))
check("empty identity reported",
      any("identity" in g for g in profile_gaps(cfg4)))

# The example config must load and be gap-free (it is the wizard's seed).
ex = load_config(ROOT / "config.example.yaml")
check("config.example.yaml loads with coverletter",
      bool(ex.coverletter.families))

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("all config coverletter tests passed")
