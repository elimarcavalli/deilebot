# WhatsApp Cloud API — operator setup

> **Audience:** the human operator who owns the Meta Business account that
> will host this bot. Everything in this document is on you — DEILE cannot
> auto-provision Meta accounts, request approvals, or maintain HTTPS infra.

This is the runbook to take the WhatsApp adapter from "code merged" to
"messages flow in production". Skip nothing — Meta rejects half-configured
deployments silently (or worse, with cryptic 132xxx error codes).

---

## 0. What you are signing up for

| Item | Reality |
|------|---------|
| **Cost per conversation** | US$ 0.05 – 0.20 depending on category and country (see Meta pricing). The first 1000 service conversations per month are free. |
| **24h conversation window** | After the user's last inbound message, you have 24h to send free text. Past that, **only approved templates pass**. |
| **Templates** | Each template you want to send must be submitted to Meta and approved (24–48h review). Marketing templates are stricter. |
| **Edit / reactions / typing** | Cloud API does not support edit (you delete + resend) or send-side reactions to messages outside a conversation. Typing is unsupported entirely. |
| **HTTPS** | Webhook endpoint must be public HTTPS with a valid certificate. No self-signed. No localhost — Meta tries to reach it from outside. |
| **Business verification** | Mandatory before you exit sandbox. Allow 1–2 weeks. |

---

## 1. Meta Business Manager — first-time provisioning

1. Create / log into a **Meta Business account** at https://business.facebook.com/.
2. Add a **WhatsApp Business Account (WABA)** under *Business Settings → Accounts → WhatsApp Accounts*.
3. Inside the WABA, add a **phone number** (the sandbox number is fine for development; production needs a real number you control and have not used on personal WhatsApp recently).
4. Go to *Business Settings → Users → System Users* and create a system user with **Admin** role on the WABA.
5. **Generate a System User access token** with the `whatsapp_business_messaging` and `whatsapp_business_management` scopes.
   - **Set the token to never expire.** This is the only token type that does. The 24h user tokens that show up by default in the developer console will **lock you out** mid-conversation.
6. Copy the token to a password manager. Meta only shows it once.

Record:

```
DEILE_BOT_WHATSAPP_ACCESS_TOKEN=<system user access token, never-expires>
DEILE_BOT_WHATSAPP_PHONE_NUMBER_ID=<from WhatsApp Manager → API Setup>
DEILE_BOT_WHATSAPP_BUSINESS_ACCOUNT_ID=<WABA id, also from API Setup>
```

---

## 2. Webhook configuration

The bot must serve a public HTTPS URL Meta can reach. The Cloud API requires:

- A `GET` handshake on first registration (token verification).
- A `POST` per inbound event (text, image, button reply, list reply, status callback).

In *WhatsApp Manager → Configuration → Webhook*:

1. **Callback URL** = `https://your-public-host/webhook/whatsapp`
2. **Verify token** = a long random string you choose. Set the same value on the bot:

```
DEILE_BOT_WHATSAPP_VERIFY_TOKEN=<long random string>
```

3. Subscribe to webhook fields: `messages` (mandatory), `message_status_updates` (recommended).

4. **Sign the payload.** In *App Dashboard → Settings → Basic*, copy the **App Secret** and set:

```
DEILE_BOT_WHATSAPP_APP_SECRET=<app secret>
```

   The bot will validate `X-Hub-Signature-256` on every inbound POST. If the
   header is missing or wrong, the request is rejected with 401 — Meta sees
   that as a misconfiguration and disables the webhook after a few failures.

If you skip the App Secret, signature verification is bypassed for development
convenience. **Do not deploy without it.**

---

## 3. Template approval workflow

Templates live in `config/whatsapp_templates.yaml` on the bot side **and**
need an identically-named approved template on Meta's side. The bot sends
the *name* + *language*; Meta resolves the body. Mismatch → error 132001.

For each template you want to use:

1. In *WhatsApp Manager → Message Templates*, click **Create Template**.
2. Pick a **category**:
   - `utility` — order updates, account changes, appointment reminders. Cheap.
   - `marketing` — promotions, offers. Expensive, strict review.
   - `authentication` — OTP, verification codes. Cheap, strict format rules.
   - `service` — replies inside the 24h window. Free.
3. Pick a **language**. You may add more languages later, each is a separate template entry (and a separate approval).
4. Write the **body**. Use `{{1}}`, `{{2}}` for variables. Provide a sample value for each — Meta requires these for review.
5. Optionally add a **header** (text or media) and **buttons** (Reply or URL).
6. Submit. Approval typically takes 30 minutes to 24 hours. Marketing can take longer.
7. After approval, **mirror the template in `config/whatsapp_templates.yaml`**:

   ```yaml
   templates:
     - name: appointment_reminder      # exact name in Meta
       language: pt_BR                  # exact locale code
       category: utility
       body_params:
         - name: patient_name           # corresponds to {{1}}
         - name: appointment_time       # corresponds to {{2}}
       header_params: []
   ```

   The catalog validates that the number of body/header params matches what
   you pass at send time, **before** the request hits Meta — saves you from
   debugging a 400 with "param count mismatch".

**Common pitfalls:**

- Adding the YAML entry without submitting to Meta → Meta returns 132001.
- Approving on Meta but forgetting the YAML → adapter sends with no
  components and Meta rejects if your template has placeholders.
- Editing an approved template's body → Meta resets it to "pending" review.
- Trying to send `marketing` from a number that is not yet
  business-verified → Meta will silently drop the message.

---

## 4. DEILE side — wire the tool

In the DEILE checkout, set:

```
DEILE_BOT_ENDPOINT=http://127.0.0.1:8765
DEILE_BOT_AUTH_TOKEN=<bearer token configured on the bot's control plane>
```

The tool `messaging.whatsapp_send_template` auto-registers when:

1. `deilebot` is importable, AND
2. Both env vars above are set.

The tool requires **explicit operator approval per call** (it is `DANGEROUS`
+ `require_approval=True`) because every send costs money. Configure your
`ApprovalSystem` to auto-grant for dev or wire it to your own confirmation
UI for production.

---

## 5. End-to-end validation (10 scenarios)

This is the bateria you must run before declaring WhatsApp "production-ready".
Spec lives at `docs/future/deilebot/whatsapp/03-FASE-E2E.md` (DEILE side).
Record results in `docs/future/deilebot/whatsapp/05-E2E-RESULTADOS.md`.

| # | Scenario | Pass criteria |
|---|---|---|
| 1 | Inbound text within window → free-text reply | Reply visible in WhatsApp client |
| 2 | Inbound image → adapter receives `Attachment(IMAGE)` | Tool sees mime + url |
| 3 | Reply Button click → `interactive.button_reply` parsed → `env.text == title`, `env.raw["interactive"]["id"] == callback_data` | Test by sending a button-bearing message and clicking |
| 4 | List selection → `interactive.list_reply` parsed identically | Same |
| 5 | Free-text outside 24h window → adapter rejects (or pipeline routes to template) | No silent send |
| 6 | Approved template send (utility, no params) | Message arrives, metric `category=utility,status=ok` increments |
| 7 | Approved template send (utility, 2 body params) | Variables substituted correctly |
| 8 | Unapproved template name → ProviderError 132001 surfaces to operator | Tool returns error, no charge |
| 9 | Param-count mismatch → ProviderError raised before HTTP call | Error message names the template |
| 10 | Marketing template send (after business verification) | Meta accepts, metric `category=marketing` increments |

A failing E2E #5 means your foundation is not actually consulting the window —
that's the most expensive failure mode (you will burn marketing-tier credits
trying to send free text).

---

## 6. Going to production

- **Business verification** — submit `WhatsApp Manager → Account Info →
  Business Verification`. Allow 1–2 weeks. Without it you stay in sandbox
  (50 unique recipients/day, marketing disabled).
- **Display name approval** — submit your business name. 24h review.
- **Webhook reliability** — Meta disables your webhook after ~5 consecutive
  failures. Run `health` on the bot's control plane from your monitor.
- **Cost dashboard** — the metric `bot_whatsapp_conversations_total{category,status}`
  is your single source of truth for spend. Wire it to whatever you graph.
- **Token rotation** — System User tokens never expire, but the *system user*
  itself can be deactivated. Have a second system user pre-created so you
  can rotate without downtime.

---

## 7. Where to get unstuck

| Symptom | Almost always means |
|---|---|
| `132001 Template name does not exist` | Name/locale typo, OR template not yet approved on Meta |
| `131056 (Re-engagement message)` | You tried free text outside 24h. Use a template. |
| `131047 Re-engagement message` | Same as above, older variant. |
| `131051 Unsupported message type` | You sent something Cloud API rejects (e.g. interactive on a non-Business number). |
| Webhook silent (no inbound events) | Verify token mismatch, or App Secret signature failing. Check bot logs. |
| `400 with no clear reason` | Often interactive payload over Meta's per-field char limit. Catalog check passes; Cloud rejects. |

Check `bot logs` first (`./data/logs/`), then `bot_outbound_total{provider=whatsapp,status=fail}` for trends. Meta does not provide a useful "why was this rejected" UI — your only signal is what the API responds.
