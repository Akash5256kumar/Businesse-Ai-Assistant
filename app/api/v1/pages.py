from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["pages"])

_PRIVACY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Privacy Policy – Hisabwalla</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                   "Helvetica Neue", Arial, sans-serif;
      background: #f6f8f7;
      color: #17211d;
      line-height: 1.7;
      font-size: 15px;
    }

    header {
      background: #16c35b;
      color: #fff;
      padding: 32px 24px 24px;
      text-align: center;
    }
    header h1 { font-size: 26px; font-weight: 700; letter-spacing: -0.3px; }
    header p  { font-size: 13px; opacity: 0.88; margin-top: 6px; }

    main {
      max-width: 720px;
      margin: 0 auto;
      padding: 32px 20px 60px;
    }

    .badge {
      display: inline-block;
      background: #dcf7e7;
      color: #0a9d45;
      font-size: 12px;
      font-weight: 600;
      border-radius: 20px;
      padding: 4px 12px;
      margin-bottom: 28px;
    }

    section { margin-bottom: 32px; }

    h2 {
      font-size: 17px;
      font-weight: 700;
      color: #0a9d45;
      margin-bottom: 10px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    h2 .icon { font-size: 20px; }

    p  { margin-bottom: 10px; color: #3a4d43; }
    ul { padding-left: 20px; margin-bottom: 10px; color: #3a4d43; }
    li { margin-bottom: 6px; }

    .card {
      background: #fff;
      border: 1px solid #e3e8e6;
      border-radius: 14px;
      padding: 20px 22px;
    }

    .highlight {
      background: #dcf7e7;
      border-left: 4px solid #16c35b;
      border-radius: 0 10px 10px 0;
      padding: 14px 16px;
      margin-bottom: 12px;
      font-size: 14px;
      color: #0a4a22;
    }

    footer {
      text-align: center;
      font-size: 12px;
      color: #98a49f;
      padding: 20px;
      border-top: 1px solid #e3e8e6;
    }

    a { color: #16c35b; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>

<header>
  <h1>🧾 Hisabwalla</h1>
  <p>Privacy Policy</p>
</header>

<main>
  <span class="badge">Last updated: June 2026</span>

  <div class="highlight">
    Your privacy matters to us. Hisabwalla is built to help small business
    owners manage their accounts — we collect only what is essential to run
    the app and never sell your data to anyone.
  </div>

  <section>
    <div class="card">
      <h2><span class="icon">📋</span> 1. Who We Are</h2>
      <p>
        Hisabwalla ("we", "our", "us") is a business account-management app
        operated by Solution Bowl. It helps shopkeepers track sales, purchases,
        customer ledgers, inventory, and send payment reminders via WhatsApp.
      </p>
      <p>
        For questions about this policy, contact us at
        <a href="mailto:support@hisabwalla.com">support@hisabwalla.com</a>.
      </p>
    </div>
  </section>

  <section>
    <div class="card">
      <h2><span class="icon">📦</span> 2. Information We Collect</h2>
      <ul>
        <li>
          <strong>Account information</strong> — your mobile number (used for
          OTP login), full name, and business name.
        </li>
        <li>
          <strong>Business data</strong> — transactions, customers, inventory
          items, and reminders that you enter into the app.
        </li>
        <li>
          <strong>Device information</strong> — device token for push
          notifications, platform (Android / iOS), and app version.
        </li>
        <li>
          <strong>Usage data</strong> — basic analytics such as feature usage
          frequency, collected via Firebase Analytics.
        </li>
      </ul>
      <p>We do <strong>not</strong> collect payment card details, passwords,
      or sensitive financial credentials.</p>
    </div>
  </section>

  <section>
    <div class="card">
      <h2><span class="icon">🎯</span> 3. How We Use Your Information</h2>
      <ul>
        <li>To authenticate you and keep your account secure.</li>
        <li>To store and sync your business records across devices.</li>
        <li>To send payment-reminder WhatsApp messages to your customers on
            your instruction.</li>
        <li>To send push notifications about important activity in your
            account.</li>
        <li>To improve app features and fix issues using anonymised
            analytics.</li>
      </ul>
    </div>
  </section>

  <section>
    <div class="card">
      <h2><span class="icon">🤝</span> 4. Third-Party Services</h2>
      <p>
        We use the following trusted third-party services. Each has its own
        privacy policy:
      </p>
      <ul>
        <li>
          <strong>Firebase (Google)</strong> — authentication, push
          notifications, and analytics.
          <a href="https://firebase.google.com/support/privacy" target="_blank">
            Privacy policy ↗</a>
        </li>
        <li>
          <strong>WhatsApp Business API</strong> — sending payment reminders.
          <a href="https://www.whatsapp.com/legal/privacy-policy" target="_blank">
            Privacy policy ↗</a>
        </li>
        <li>
          <strong>DigitalOcean</strong> — cloud hosting for our servers and
          database.
          <a href="https://www.digitalocean.com/legal/privacy-policy" target="_blank">
            Privacy policy ↗</a>
        </li>
        <li>
          <strong>Anthropic / Claude AI</strong> — AI assistant features
          (voice and text input for transactions). Inputs are processed to
          generate responses and are not used to train models.
          <a href="https://www.anthropic.com/privacy" target="_blank">
            Privacy policy ↗</a>
        </li>
      </ul>
    </div>
  </section>

  <section>
    <div class="card">
      <h2><span class="icon">🔒</span> 5. Data Security</h2>
      <p>
        All data is transmitted over HTTPS. Your business data is stored in an
        encrypted PostgreSQL database hosted on DigitalOcean. Access to the
        database is restricted to authorised personnel only.
      </p>
      <p>
        Push notification tokens are stored securely and used only to deliver
        notifications to your own device.
      </p>
    </div>
  </section>

  <section>
    <div class="card">
      <h2><span class="icon">👥</span> 6. Data Sharing</h2>
      <p>We do <strong>not</strong> sell, rent, or trade your personal
      information. We share data only:</p>
      <ul>
        <li>With the third-party services listed above, to the extent needed
            to operate the app.</li>
        <li>If required by law, court order, or to protect our legal
            rights.</li>
      </ul>
    </div>
  </section>

  <section>
    <div class="card">
      <h2><span class="icon">🗂️</span> 7. Data Retention</h2>
      <p>
        Your data is retained for as long as your account is active. If you
        delete your account, we will permanently delete your personal data
        within 30 days, except where we are required by law to retain it.
      </p>
    </div>
  </section>

  <section>
    <div class="card">
      <h2><span class="icon">✅</span> 8. Your Rights</h2>
      <ul>
        <li><strong>Access</strong> — request a copy of the data we hold about
            you.</li>
        <li><strong>Correction</strong> — update incorrect personal
            information.</li>
        <li><strong>Deletion</strong> — request permanent deletion of your
            account and data.</li>
        <li><strong>Opt-out</strong> — disable push notifications at any time
            from your device settings.</li>
      </ul>
      <p>
        To exercise any of these rights, contact us at
        <a href="mailto:support@hisabwalla.com">support@hisabwalla.com</a>.
      </p>
    </div>
  </section>

  <section>
    <div class="card">
      <h2><span class="icon">👶</span> 9. Children's Privacy</h2>
      <p>
        Hisabwalla is intended for business owners and is not directed at
        children under the age of 13. We do not knowingly collect personal
        information from children.
      </p>
    </div>
  </section>

  <section>
    <div class="card">
      <h2><span class="icon">🔄</span> 10. Changes to This Policy</h2>
      <p>
        We may update this Privacy Policy from time to time. When we do, we
        will update the "Last updated" date at the top. For significant
        changes, we will notify you via a push notification or an in-app
        message.
      </p>
    </div>
  </section>

  <section>
    <div class="card">
      <h2><span class="icon">📬</span> 11. Contact Us</h2>
      <p>
        If you have any questions, concerns, or requests regarding this Privacy
        Policy, please reach out:
      </p>
      <ul>
        <li>Email: <a href="mailto:support@hisabwalla.com">support@hisabwalla.com</a></li>
        <li>App: Profile → Help &amp; Support</li>
      </ul>
    </div>
  </section>
</main>

<footer>
  &copy; 2026 Hisabwalla · Solution Bowl · All rights reserved
</footer>

</body>
</html>"""


@router.get("/privacy-policy", response_class=HTMLResponse)
async def privacy_policy() -> HTMLResponse:
    return HTMLResponse(content=_PRIVACY_HTML)
