import json
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("talentscope")
BUNDLE = Path(__file__).resolve().parents[1] / "vendor" / "skill_match.cjs"
BATCH = 400


class SkillMatchError(Exception):
    pass


def available() -> bool:
    return BUNDLE.is_file() and shutil.which("node") is not None


def _run(jd_text: str, people: list[dict]) -> list[dict]:
    node = shutil.which("node")
    if not node:
        raise SkillMatchError("Node.js is required for JD matching (install Node 18+ and restart the backend).")
    if not BUNDLE.is_file():
        raise SkillMatchError("python/vendor/skill_match.cjs is missing. Run `npm run build:skill-match` in UI/.")
    try:
        done = subprocess.run([node, str(BUNDLE)], input=json.dumps({"jdText": jd_text, "people": people}), capture_output=True,
                              text=True, encoding="utf-8", timeout=120)
    except subprocess.TimeoutExpired as err:
        raise SkillMatchError("JD matching timed out") from err
    if done.returncode != 0:
        log.error("skill_match failed: %s", done.stderr[:400])
        raise SkillMatchError("JD matching failed")
    return json.loads(done.stdout)


def person_input(emp: dict) -> dict:
    return {"skills": emp.get("skills"), "designation": emp.get("designation"), "grade": emp.get("grade"),
            "product": emp.get("product"), "role": emp.get("role"), "experience": emp.get("experience"),
            "yearsExperience": emp.get("yearsExperience", emp.get("years_experience"))}


def score_people(jd_text: str, people: list[dict]) -> list[dict]:
    out: list[dict] = []
    for start in range(0, len(people), BATCH):
        out.extend(_run(jd_text, [person_input(p) for p in people[start:start + BATCH]]))
    return out
