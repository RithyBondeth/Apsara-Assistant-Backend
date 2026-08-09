# Connecting Messenger and Telegram

Everything in the webhook path is written to Meta's and Telegram's documented
formats and covered by tests, but **none of it has been exercised against the
live platforms**. Doing that needs credentials and a public HTTPS URL. This is
the shortest path from here to knowing it works.

Work through Telegram first. It takes about five minutes, needs no app review,
and proves the whole pipeline — signature check, queue, customer creation,
reply delivery — before you spend an afternoon on Meta.

---

## What you need first

**A public HTTPS URL for the API.** Both platforms refuse `localhost`, and
Telegram refuses plain HTTP. While developing, a tunnel is fine:

```bash
cloudflared tunnel --url http://localhost:8000
```

Set the address it prints as `API_BASE_URL`, then restart the API — the webhook
URLs shown in the app are built from it.

---

## Telegram

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the
   token it gives you.
2. In the app, **Integrations → Connect Telegram**. Put the bot's username in
   *Bot ID* and the token in *Bot token*.
3. Press **Test connection**. It should report the bot's `@handle`. If it does
   not, the token is wrong — nothing further will work.
4. Press **Register webhook**. This calls `setWebhook` for you with the URL and
   secret; there is no need to curl it by hand.
5. Message your bot from your own Telegram account.

**Expected:** the conversation appears under Chat within a second or two, named
from your Telegram profile, and — if `OPENAI_API_KEY` is set and the connection
has auto-reply on — the assistant answers in the same language you wrote in.

If nothing arrives, ask Telegram what it thinks:

```bash
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

`last_error_message` is usually the whole story — an unreachable URL, or a TLS
problem with the tunnel.

---

## Messenger

Slower, because Meta requires an app and, for anything beyond your own test
users, review.

1. Create an app at [developers.facebook.com](https://developers.facebook.com/)
   and add the **Messenger** product.
2. Under **App settings → Basic**, copy the **App Secret** into
   `META_APP_SECRET`. This is what signs webhook payloads; without it every
   delivery is rejected as unsigned.
3. Invent any string, put it in `META_VERIFY_TOKEN`, and restart the API.
4. Under **Messenger → Settings**, generate a **Page access token** for your
   Page and note the **Page ID**.
5. In the app, **Integrations → Connect Facebook Messenger**, with those two
   values. Press **Test connection** — it should report the Page's name.
6. Back in the Meta dashboard, under **Webhooks**, add a callback URL of
   `<API_BASE_URL>/api/v1/webhooks/messenger` and your verify token, then press
   **Verify and Save**. Meta calls the endpoint immediately and expects the
   challenge echoed back.
7. **Subscribe the webhook to the `messages` field**, and subscribe your Page.
   This step is easy to miss and produces exactly the same symptom as a broken
   webhook: silence.
8. Message your Page from an account that is an admin, developer or tester of
   the app. Other accounts will not reach an unreviewed app.

**Expected:** as with Telegram. Note that a first-contact customer may appear as
`Customer 123456` — the name lookup needs a permission not every account grants,
and the placeholder is the intended fallback.

---

## When something is silent

Silence is the common failure, and it has several causes that look identical
from the outside. In rough order of likelihood:

| Check | How |
|---|---|
| Did the platform call us at all? | Look for `POST /api/v1/webhooks/...` in the API log |
| Was it rejected as unsigned? | A `403` in that log means `META_APP_SECRET` is wrong |
| Was the work queued? | `select kind, status, attempts, last_error from jobs order by created_at desc limit 5;` |
| Is anything running the queue? | With `JOB_RUNNER=worker`, a worker must be up; with `inline`, the API does it |
| Was a reply generated but refused? | `Reply generated but not delivered` in the log, with the platform's reason |
| Is the assistant switched off? | Auto-reply toggle on the connection |
| Is the day's allowance spent? | `Daily reply limit reached` in the log |

A job stuck in `pending` with a rising `attempts` and a `last_error` is the most
informative failure available — read that before anything else.
