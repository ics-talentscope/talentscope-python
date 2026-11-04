from html import escape


def build_invite_html(full_name: str, login_hint: str, portal_url: str, label: str, password: str = "") -> str:
    name, url, lab = escape(full_name), escape(portal_url, quote=True), escape(label)
    return f"""
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 30px; border: 1px solid #e0e7ff; border-radius: 16px; background-color: #ffffff;">
  <div style="text-align: center; margin-bottom: 24px;">
    <div style="display: inline-block; width: 48px; height: 48px; border-radius: 12px; background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%); line-height: 48px; color: #ffffff; font-size: 20px; font-weight: bold;">TS</div>
    <h2 style="color: #1e1b4b; margin-top: 12px; margin-bottom: 4px; font-size: 20px; font-weight: 800;">TalentScope</h2>
    <span style="color: #6b7280; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em;">Assessment Invitation</span>
  </div>
  <hr style="border: 0; border-top: 1px solid #f3f4f6; margin-bottom: 24px;" />
  <div style="color: #374151; font-size: 14px; line-height: 1.6; font-weight: 500;">
    <p>Dear <strong>{name}</strong>,</p>
    <p>Congratulations! Our HR team evaluated your profile against one of our open position's Job, and classified you as a <strong>Highly Suitable</strong> candidate.</p>
    <p>We are excited to invite you to the next step of our recruitment process: a secure, voice-assisted {lab} evaluation on our screening portal.</p>
    <div style="margin: 28px 0; text-align: center;">
      <a href="{url}" target="_blank" role="button" style="display: inline-block; padding: 12px 32px; background-color: #4f46e5; color: #ffffff; text-decoration: none; border-radius: 10px; font-weight: bold; font-size: 14px;">Start Assessment</a>
    </div>
    <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px; margin: 20px 0;">
      <h4 style="color: #1e293b; margin: 0 0 8px 0; font-size: 13px; font-weight: 700;">Instructions for logging in:</h4>
      <ul style="margin: 0; padding-left: 20px; font-size: 12px; color: #475569;">
        <li style="margin-bottom: 4px;">Go to: <a href="{url}" style="color: #4f46e5; text-decoration: underline;">{escape(portal_url)}</a></li>
        <li style="margin-bottom: 4px;">{login_hint}</li>
        <li style="margin-bottom: 4px;">Temporary password: <strong>{escape(password)}</strong></li>
        <li style="margin-bottom: 4px;">Use your registered email address and this password to sign in.</li>
        <li>You will have exactly <strong>one attempt</strong> to take the {lab} evaluation. Please make sure you are in a quiet room with a working microphone.</li>
      </ul>
    </div>
    <p style="margin-top: 24px;">Good luck with your interview!</p>
    <p style="color: #9ca3af; font-size: 12px; margin-top: 32px; border-top: 1px solid #f3f4f6; padding-top: 16px; text-align: center;">This is an automated email dispatched by TalentScope.</p>
  </div>
</div>
"""
