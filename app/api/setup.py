"""
Yahoo OAuth setup flow.

Two paths:
  Automatic: /setup/authorize → Yahoo → /setup/callback (requires HTTPS redirect URI)
  Manual:    /setup/authorize → Yahoo → browser shows failed redirect with ?code=XXX
             → user visits /setup/manual and pastes the code or full URL
"""

from urllib.parse import urlparse, parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.yahoo_service import build_authorization_url, exchange_code_for_tokens

router = APIRouter()

_SUCCESS_HTML = """
<html>
<head><title>Authorized!</title>
<style>
  body{{font-family:-apple-system,sans-serif;background:#09090f;color:#e2e8f0;
       max-width:560px;margin:80px auto;text-align:center;padding:24px;}}
  h2{{color:#10b981;margin-bottom:10px;}}
  p{{color:#94a3b8;margin-bottom:24px;}}
  .btn{{display:inline-block;padding:12px 24px;background:#6c63ff;color:#fff;
        border-radius:8px;text-decoration:none;margin:6px;font-weight:600;}}
</style>
</head>
<body>
<h2>&#10003; Yahoo authorization successful</h2>
<p>Tokens stored. Head to the admin panel to run the initial data load.</p>
<a class="btn" href="/admin.html">Go to Admin Panel</a>
<a class="btn" href="/">Go to Dashboard</a>
</body>
</html>
"""

_MANUAL_HTML = """
<html>
<head><title>Complete Yahoo Setup</title>
<style>
  body{{font-family:-apple-system,sans-serif;background:#09090f;color:#e2e8f0;
       max-width:600px;margin:60px auto;padding:24px;}}
  h2{{color:#a78bfa;margin-bottom:6px;}}
  p{{color:#94a3b8;font-size:0.9rem;line-height:1.6;}}
  ol{{color:#94a3b8;font-size:0.9rem;padding-left:20px;line-height:2;}}
  .box{{background:#111118;border:1px solid #2a2a3a;border-radius:10px;padding:20px;margin:20px 0;}}
  code{{background:#1a1a26;padding:2px 6px;border-radius:4px;font-size:0.85rem;color:#60a5fa;}}
  input{{background:#1a1a26;border:1px solid #2a2a3a;color:#e2e8f0;padding:10px 14px;
         border-radius:8px;width:100%;box-sizing:border-box;font-size:0.9rem;margin:8px 0;}}
  input:focus{{outline:2px solid #6c63ff;border-color:transparent;}}
  button{{background:#6c63ff;color:white;border:none;padding:11px 24px;border-radius:8px;
          font-size:0.95rem;font-weight:600;cursor:pointer;margin-top:8px;width:100%;}}
  button:hover{{opacity:0.88;}}
  .err{{background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.3);
        color:#ef4444;padding:10px 14px;border-radius:8px;margin-top:12px;font-size:0.85rem;}}
  a{{color:#6c63ff;}}
</style>
</head>
<body>
<h2>Complete Yahoo Authorization</h2>
<p>Yahoo requires HTTPS for redirect URLs, so the automatic redirect didn't come back here.
   Follow these steps to finish authorization:</p>

<div class="box">
  <ol>
    <li><a href="/setup/authorize" target="_blank">Click here to open the Yahoo authorization page</a></li>
    <li>Sign in and click <strong>Agree</strong></li>
    <li>Yahoo will try to redirect to your callback URL &mdash; the browser will show a
        <strong>connection error</strong> or blank page. <strong>That's expected.</strong></li>
    <li>Copy the <strong>full URL</strong> from your browser's address bar (it contains <code>?code=...</code>)</li>
    <li>Paste it below and click <strong>Complete Setup</strong></li>
  </ol>
</div>

<input type="text" id="code-input"
  placeholder="Paste the full redirect URL or just the code value here" />
{error_block}
<button onclick="submit()">Complete Setup</button>

<script>
function submit() {{
  const val = document.getElementById('code-input').value.trim();
  if (!val) return;
  let code = val;
  // If user pasted a full URL, extract the code param
  try {{
    const url = new URL(val);
    const c = url.searchParams.get('code');
    if (c) code = c;
  }} catch(e) {{}}
  window.location = '/setup/exchange?code=' + encodeURIComponent(code);
}}
document.getElementById('code-input').addEventListener('keydown', e => {{
  if (e.key === 'Enter') submit();
}});
</script>
</body>
</html>
"""


@router.get("/authorize")
async def start_oauth():
    url = build_authorization_url(state="setup")
    return RedirectResponse(url=url)


@router.get("/manual")
async def manual_entry(error: str = ""):
    error_block = f'<div class="err">{error}</div>' if error else ""
    return HTMLResponse(_MANUAL_HTML.format(error_block=error_block))


@router.get("/exchange")
async def exchange_code(
    code: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Receives the auth code (from manual entry or automatic callback) and exchanges it."""
    if not code:
        return HTMLResponse(
            '<p>No code provided. <a href="/setup/manual">Try again</a></p>',
            status_code=400,
        )
    try:
        await exchange_code_for_tokens(code, db)
    except Exception as exc:
        from urllib.parse import quote
        err = quote(str(exc))
        return RedirectResponse(f"/setup/manual?error={err}")

    return HTMLResponse(_SUCCESS_HTML)


@router.get("/callback")
async def oauth_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    code: str = "",
    error: str = "",
):
    """Handles automatic redirect if the callback URL is reachable (e.g. on TrueNAS with HTTPS)."""
    if error:
        from urllib.parse import quote
        return RedirectResponse(f"/setup/manual?error={quote(error)}")
    if not code:
        return RedirectResponse("/setup/manual")
    return RedirectResponse(f"/setup/exchange?code={code}")
