"""Employee Learning Portal product-assessment invite email HTML."""
from __future__ import annotations

import os
from html import escape


def build_portal_invite_html(
    full_name: str,
    employee_id: str,
    temporary_password: str | None = None,
    portal_url: str = "https://talentscope.infinite.com/",
    deadline: str | None = None,
    product_hint: str = "for example, HLR-HSS",
) -> str:
    name = escape(full_name or "Employee")
    emp_id = escape(employee_id or "")
    pwd = escape(temporary_password or employee_id or "")
    url = escape((portal_url or "https://talentscope.infinite.com/").rstrip("/") + "/", quote=True)
    url_text = escape((portal_url or "https://talentscope.infinite.com/").rstrip("/") + "/")
    raw_due = (deadline or os.environ.get("PRODUCT_ASSESSMENT_DEADLINE") or "").strip()
    due = escape(raw_due) if raw_due else ""
    hint = escape(product_hint or "for example, HLR-HSS")
    due_line = (
        f'<p style="margin-top:20px;"><strong>Please complete your assigned Product Assessment by {due}.</strong></p>'
        if due
        else '<p style="margin-top:20px;"><strong>Please complete your assigned Product Assessment promptly.</strong></p>'
    )

    def rule(rule: str, req: str) -> str:
        return (
            "<tr>"
            f'<td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;font-size:12px;color:#1e293b;font-weight:700;vertical-align:top;width:34%;">{escape(rule)}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;font-size:12px;color:#475569;vertical-align:top;">{escape(req)}</td>'
            "</tr>"
        )

    rules = "".join(
        [
            rule("Stay on the assessment screen", "Do not switch tabs, minimize the browser, or open other applications."),
            rule("Keep your face visible", "Your face must remain within the camera frame throughout the assessment."),
            rule("No external assistance", "Multiple people appearing in the camera frame will be flagged."),
            rule("No copy/paste", "Copy, paste, right-click, keyboard shortcuts, and Developer Tools are disabled."),
            rule("Do not refresh or close the browser", "Refreshing or closing the assessment window may interrupt your attempt."),
            rule("Video recording", "Your assessment session may be recorded for review purposes."),
        ]
    )

    return f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;max-width:680px;margin:0 auto;padding:28px;border:1px solid #e0e7ff;border-radius:16px;background:#fff;color:#374151;font-size:14px;line-height:1.6;">
  <div style="text-align:center;margin-bottom:20px;">
    <div style="display:inline-block;width:48px;height:48px;border-radius:12px;background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 100%);line-height:48px;color:#fff;font-size:20px;font-weight:bold;">TS</div>
    <h2 style="color:#1e1b4b;margin:12px 0 4px;font-size:20px;font-weight:800;">TalentScope</h2>
    <span style="color:#6b7280;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;">Employee Learning Portal</span>
  </div>
  <hr style="border:0;border-top:1px solid #f3f4f6;margin-bottom:20px;" />
  <p>Dear <strong>{name}</strong>,</p>
  <p>You have been assigned a <strong>Product Assessment</strong> on the Employee Learning Portal. Please read the instructions below carefully before starting your assessment.</p>
  <h3 style="color:#1e1b4b;font-size:15px;margin:24px 0 8px;">Employee Learning Portal</h3>
  <p><strong>Portal Link:</strong> <a href="{url}" style="color:#4f46e5;">{url_text}</a></p>
  <div style="margin:16px 0;text-align:center;">
    <a href="{url}" target="_blank" style="display:inline-block;padding:12px 28px;background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 100%);color:#fff;text-decoration:none;border-radius:10px;font-weight:bold;font-size:14px;">Open Employee Portal</a>
  </div>
  <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:14px 16px;margin:16px 0;">
    <h4 style="margin:0 0 8px;font-size:13px;color:#1e293b;">Login Credentials</h4>
    <p style="margin:0 0 4px;font-size:13px;"><strong>Employee ID:</strong> {emp_id}</p>
    <p style="margin:0;font-size:13px;"><strong>Temporary Password:</strong> {pwd}</p>
  </div>
  <h3 style="color:#1e1b4b;font-size:15px;margin:24px 0 8px;">Before You Begin</h3>
  <ul style="padding-left:20px;font-size:13px;color:#475569;">
    <li>Use <strong>Google Chrome</strong> or <strong>Microsoft Edge</strong>. Safari and Firefox are not supported for camera recording.</li>
    <li>Use a laptop or desktop with a functioning webcam and a stable internet connection.</li>
    <li>Sit in a quiet, well-lit environment where you are alone and clearly visible on camera.</li>
    <li>Keep your Employee ID and Password readily available.</li>
    <li>Reserve <strong>30–45 minutes</strong> of uninterrupted time. The assessment contains <strong>25 questions</strong> with a <strong>30-minute</strong> time limit.</li>
    <li>Do not use a mobile phone during the assessment. Looking away from the screen or at a phone may be recorded as a proctoring violation.</li>
  </ul>
  <h3 style="color:#1e1b4b;font-size:15px;margin:24px 0 8px;">Assessment Instructions</h3>
  <p><strong>Step 1 – Log In</strong></p>
  <ul style="padding-left:20px;font-size:13px;color:#475569;"><li>Open the Employee Learning Portal.</li><li>Enter your Employee ID and Password.</li><li>Click Sign In.</li></ul>
  <p><strong>Step 2 – Open Your Dashboard</strong></p>
  <p style="font-size:13px;color:#475569;">After logging in, you will be directed to your dashboard where you will see your assigned Product Assessment ({hint}).</p>
  <p><strong>Step 3 – Start the Assessment</strong></p>
  <ul style="padding-left:20px;font-size:13px;color:#475569;"><li>Click <strong>Start Assessment</strong>.</li><li>If you have already started, click <strong>Resume Assessment</strong>.</li></ul>
  <p><strong>Step 4 – Grant Required Permissions</strong></p>
  <ul style="padding-left:20px;font-size:13px;color:#475569;"><li>Camera/Webcam access</li><li>Fullscreen mode</li></ul>
  <p style="font-size:13px;color:#475569;">These permissions are mandatory for the assessment.</p>
  <p><strong>Step 5 – Read the Assessment Rules</strong></p>
  <p style="font-size:13px;color:#475569;">Review the guidelines and integrity agreement, then click Start Assessment.</p>
  <p><strong>Step 6 – Complete the Questions</strong></p>
  <ul style="padding-left:20px;font-size:13px;color:#475569;"><li>Answer all 25 questions.</li><li>Use Previous and Next to navigate.</li><li>Select one answer for each question.</li><li>Flag questions to review later.</li><li>Watch the timer at the top of the page.</li></ul>
  <p><strong>Step 7 – Submit Your Assessment</strong></p>
  <ul style="padding-left:20px;font-size:13px;color:#475569;"><li>Ensure all questions are answered.</li><li>Click <strong>Submit</strong> (top-right).</li><li>Wait until Submitting completes.</li><li>Your score and results will be displayed.</li></ul>
  <p><strong>Step 8 – Return to Dashboard</strong></p>
  <ul style="padding-left:20px;font-size:13px;color:#475569;"><li>Assessment Score</li><li>Readiness Metrics</li><li>Assessment History</li></ul>
  <h3 style="color:#1e1b4b;font-size:15px;margin:24px 0 8px;">Proctoring Rules</h3>
  <p style="font-size:13px;color:#475569;">This assessment is monitored for integrity. Please follow the rules below.</p>
  <table style="width:100%;border-collapse:collapse;margin:12px 0 16px;background:#f8fafc;border:1px solid #e2e8f0;">
    <thead><tr style="background:#eef2ff;"><th style="text-align:left;padding:10px 12px;font-size:12px;color:#312e81;">Rule</th><th style="text-align:left;padding:10px 12px;font-size:12px;color:#312e81;">Requirement</th></tr></thead>
    <tbody>{rules}</tbody>
  </table>
  <p style="font-size:13px;color:#b91c1c;"><strong>Violations:</strong> Accumulating <strong>5 proctoring violations</strong> will automatically submit your assessment.</p>
  <h3 style="color:#1e1b4b;font-size:15px;margin:24px 0 8px;">Tips for a Smooth Assessment</h3>
  <ul style="padding-left:20px;font-size:13px;color:#475569;">
    <li>Complete the assessment in a single sitting whenever possible.</li>
    <li>If you lose internet, log back in and select Resume Assessment.</li>
    <li>Use only the Submit button at the top — there is no separate Finish button.</li>
    <li>After submission, wait for confirmation before closing the browser (video may still upload).</li>
    <li>Mobile phones are not supported.</li>
  </ul>
  <h3 style="color:#1e1b4b;font-size:15px;margin:24px 0 8px;">Need Assistance?</h3>
  <p style="font-size:13px;color:#475569;"><strong>Support Team person:</strong> Kavan Anselm<br/>
  <strong>Email:</strong> <a href="mailto:Kavan.Anselm2@infinite.com" style="color:#4f46e5;">Kavan.Anselm2@infinite.com</a></p>
  <p style="font-size:13px;color:#475569;">When contacting support, include Employee ID, browser, and a screenshot if available.</p>
  {due_line}
  <p>Thank you for your cooperation.</p>
  <p style="margin-top:20px;">Regards,<br/><strong>TalentScope Team</strong></p>
  <p style="color:#9ca3af;font-size:11px;margin-top:28px;border-top:1px solid #f3f4f6;padding-top:14px;text-align:center;">This is an automated email from TalentScope · Infinite</p>
</div>
"""
