from services import resumes

SAMPLE = """Jane Doe
Senior Software Engineer
jane.doe@example.com | +1 555 123 4567 | linkedin.com/in/janedoe | github.com/janedoe

Summary
Engineer with Python and React experience.

Experience
Senior Engineer at Acme Corp Jan 2019 - Present
- Built APIs in Python and Docker
- Led a team of 4
Developer - Beta LLC 2016 - 2018
- Wrote React frontends

Education
BSc Computer Science, State University 2015

Projects
Talent Tool - internal app
- Used python and aws

Certifications
- AWS Certified Developer
"""


def test_parse_structure_extracts_sections():
    parsed = resumes.parse_structure(SAMPLE)
    assert parsed["personal"]["email"] == "jane.doe@example.com"
    assert parsed["personal"]["linkedin"] == "https://linkedin.com/in/janedoe"
    assert len(parsed["experience"]) == 2
    first = parsed["experience"][0]
    assert first["current"] is True and first["position"] == "Senior Engineer" and first["company"].startswith("Acme")
    assert len(first["bulletPoints"]) == 2
    assert parsed["education"][0]["graduationDate"] == "2015"
    assert parsed["projects"][0]["name"] == "Talent Tool"
    assert "python" in parsed["skills"]["technical"] and "react" in parsed["skills"]["technical"]
    assert parsed["certifications"][0]["name"] == "AWS Certified Developer"


def test_skills_only_include_source_mentions_with_safe_boundaries():
    skills = resumes._skills("Python, JavaScript, and J2EE with ASP.NET and C# experience.")

    assert skills["technical"] == ["javascript", "python", "j2ee", "c#", ".net"]
    assert skills["soft"] == []
    assert skills["tools"] == []
    assert "java" not in skills["technical"]


def test_resume_row_replaces_stale_skills_with_source_verified_skills():
    resume = resumes._map_row({
        "id": "resume-1",
        "text_content": "Python developer",
        "parsed": {
            "skills": {
                "technical": ["j2ee"],
                "tools": ["git", "docker", "jira"],
                "soft": ["communication", "leadership"],
            }
        },
    })

    assert resume["parsed"]["skills"] == {
        "technical": ["python"],
        "soft": [],
        "tools": [],
        "languages": [],
        "other": [],
    }


def test_generate_report_thresholds():
    parsed = resumes.parse_structure(SAMPLE)
    report = resumes.generate_report(parsed, {"overallScore": 72, "jdMatchScore": 45, "weaknesses": [
        {"location": "Experience Section", "severity": "high", "description": "No metrics. More text", "suggestion": "Add numbers"}]})
    assert report["hiringConfidence"] == "high"
    assert report["suitability"] == "suitable" and report["jdMatchScore"] == 45
    assert report["priorityImprovements"][0]["title"] == "No metrics"
    low = resumes.generate_report(parsed, {"overallScore": 30, "jdMatchScore": 10})
    assert low["suitability"] == "unsuitable" and low["hiringConfidence"] == "low"


def test_process_resume_roundtrip_with_stubbed_ai(store, monkeypatch):
    from services import ai

    monkeypatch.setattr(ai, "analyze_resume", lambda text, parsed, jd=None: {"overallScore": 80, "jdMatchScore": 66, "weaknesses": []})
    monkeypatch.setattr("services.storage.save", lambda bucket, name, data: name)
    resume = resumes.process_resume("jane.txt", SAMPLE.encode(), jd_text="Python dev", jd_id="jd1", rm_email="rm@infinite.com", wait=True)
    assert resume["status"] == "completed" and resume["report"]["jdId"] == "jd1"
    loaded = resumes.get_resume(resume["id"], force_fresh=True)
    assert loaded["report"]["rmEmail"] == "rm@infinite.com" and loaded["parsed"]["personal"]["fullName"] == "Jane Doe"
    again = resumes.process_resume("jane.txt", SAMPLE.encode(), wait=True)
    assert again["id"] == resume["id"]
    assert len(resumes.list_resumes()) == 1
