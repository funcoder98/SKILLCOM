# Skillcom — rebuilt

## What changed from your original version

1. **Real database-backed auth, no more MetaMask.**
   `register.html`
    `/api/register`
   (`skillcom_db.users`). You're then sent to `login.html`, which POSTs to
   `/api/login`, 
   `localStorage`
   `Authorization: Bearer <token>`
   `main.py`
   `users_db`, `sessions_db`and `messages_db` 
2.fixed_skills
   `TECHNICAL_SKILLS` (`Python`, `React`, `PyTorch`,
   `Solidity`, etc.) `GET /api/skills`. Registration and booking
   forms use `<select multiple>` dropdowns populated from that list instead
   of free-text input, so "js" / "JavaScript" / "javascript" can't fragment
   your matches.

3. **Instagram-style home feed with search.** `home.html` 
`GET /api/users/search?q=...`
   registration, served from `/uploads/...`
   (via `sentence-transformers`) .

(`/api/messages`)
 `/api/sessions/propose`
 `CONFIRMED` in MongoDB,
     button (a real PDF, generated server-side with `reportlab`)e

6. `home.html`
   `Meetings` (`meetings.html` + `GET /api/meetings`) 
   status` + `session_time`, not stored
   separately, so a session moves itself from Arranged to Occurred the
   moment its time passes. Settings holds a dark/light toggle (persisted in
   `localStorage`, applied instantly with no flash on page load via a tiny
   inline script in every page's `<head>`) — dark keeps the original
   near-black/purple/white look, light switches to a white background with
   black text and a slightly deeper purple accent for contrast. Every page
   also now shows a thin purple loading bar across the top during page load
   and any in-flight request, with a small icon (💻 / 🏗️ / 📼) cycling along
   it — handled by `frontend/app-ui.js`, which every page includes.

7. **Admin dashboard, email-verified password reset, and skill-teaching
   validation.**
   - **Admin panel**: a default admin account (`admin` / `root` — seeded
     automatically on first startup, override via `SKILLCOM_ADMIN_USERNAME`
     / `SKILLCOM_ADMIN_PASSWORD`, change the password from the dashboard
     afterwards) logs in through the same `login.html` form as everyone
     else, but is routed to `admin-dashboard.html` instead of the student
     home feed. It has three tabs, all backed by new endpoints:
     **Members** (every registered user, with a **Reject**/**Reinstate**
     toggle that immediately blocks/restores login access — enforced both
     at login and on every authenticated request, so a rejected member is
     cut off mid-session too), **New Logins** (last 24 hours), and
     **All Logins** (full history) — both read from a new `login_events`
     collection written on every successful login.
   - **Change password by email**: registration now also collects an
     email. `login.html` has a "Forgot your password?" link
     (`forgot-password.html`) that emails a reset link if the address
     matches an account; `profile.html` has a "Change Password" button
     that sends the same kind of link straight to the email already on
     file, no re-typing needed. Either way the link lands on
     `reset-password.html`, which verifies the token server-side before
     letting you set a new password, then returns you to `login.html`.
     Reset tokens expire after 30 minutes. Actually sending the email
     requires SMTP credentials (`SKILLCOM_SMTP_*` in `.env.example`) —
     without them, the link is printed to the uvicorn console instead, so
     the whole flow is testable without a real mail account.
   - **Skill-teaching validation**: `booking.html`'s "Your Skill Offer"
     dropdown now only lists skills *you've* selected as ones you teach
     (not the full skill catalog), and "Requested Learning Topic" only
     lists skills the *peer* has listed as theirs — enforced again
     server-side in `POST /api/sessions/propose`, so a session simply
     can't be created offering a skill you haven't claimed.

## Project layout

```
skillcom/
  backend/
    main.py            FastAPI app (auth, search, matching, chat, sessions, receipts, on-chain minting)
    requirements.txt
    .env.example        copy to .env and fill in (Mongo, JWT secret, optional chain config)
    uploads/            profile photos land here
  contracts/
    SkillcomReputationToken.sol   the ERC-721 reputation token, soulbound
  frontend/
    index.html          redirects to login.html
    login.html
    register.html
    home.html            search + AI-match feed
    peer-profile.html    peer info + chat + Exchange button
    booking.html         propose a session
    session.html         mutual agreement + confirmation popup + receipt
    profile.html         your own tokens + session history + email/password
    meetings.html        pending / arranged / occurred sessions, in tabs
    forgot-password.html request a password reset link by email
    reset-password.html  set a new password from an emailed reset link
    admin-dashboard.html members / new logins / all logins (admin only)
    style.css
    config.js            API_BASE + auth helpers shared by every page
    app-ui.js            theme toggle, top loading bar, drawer menu - shared by every page
```

## Running it

1. **MongoDB** must be running locally (`mongodb://localhost:27017/` by
   default — override with the `SKILLCOM_MONGO_URI` env var).

2. **Backend**
   ```bash
   cd backend
   pip install -r requirements.txt
   uvicorn main:app --reload
   ```
   The first request that needs the AI matcher will download the
   `all-MiniLM-L6-v2` sentence-transformers model (~90MB) — that's normal.

3. **Frontend** — these are static files, so just open `frontend/index.html`
   in a browser, or serve the folder (e.g. `python -m http.server` from
   inside `frontend/`). It talks to the backend at `http://127.0.0.1:8000`
   (`frontend/config.js` — change `API_BASE` there if you deploy the API
   elsewhere).

4. Set a real `SKILLCOM_SECRET_KEY` env var before deploying anywhere real —
   the default in `main.py` is only for local development.

## A couple of things worth knowing

- Passwords are bcrypt-hashed server-side; the plaintext password never
  touches MongoDB.
- The chat and session-status views poll every 3 seconds rather than using
  websockets — simple and reliable for a small peer group; swap in a
  websocket/SSE endpoint later if you want it truly real-time.
- Reputation tokens are minted **for real, on-chain**, using
  `contracts/SkillcomReputationToken.sol` — a soulbound (non-transferable)
  ERC-721. Auth stayed username/password (no wallet needed to use Skillcom
  at all); wallets are opt-in, just for *receiving* tokens.

## Turning on real on-chain minting

By default, tokens are recorded off-chain only (in MongoDB) — the app works
fully without any blockchain setup. To make them real NFTs:

1. **Deploy the contract.** `contracts/SkillcomReputationToken.sol` is a
   small OpenZeppelin-based ERC-721 where only the owner (the platform
   wallet) can call `mintToken(to, skillName, sessionId)`, and tokens can't
   be transferred once minted (so they stay an honest record of who actually
   did the work). Deploy it to a testnet like Sepolia via Remix or Hardhat,
   passing your platform wallet's address as `initialOwner`.

2. **Fund the platform wallet** with a little testnet ETH (e.g. from a
   Sepolia faucet) — it pays the gas for every mint, so users never need
   their own ETH.

3. **Fill in `backend/.env.example`** (copy it to `.env` or export the vars):
   `SKILLCOM_CHAIN_RPC_URL` (from Alchemy/Infura), `SKILLCOM_CONTRACT_ADDRESS`
   (from step 1), `SKILLCOM_PLATFORM_PRIVATE_KEY` (the platform wallet that
   pays gas — **never** commit this), and optionally `SKILLCOM_CHAIN_ID` /
   `SKILLCOM_BLOCK_EXPLORER_BASE` if you're not using Sepolia.

4. **Users link a wallet.** On `profile.html`, "Connect Wallet" just reads
   the MetaMask address (`eth_requestAccounts`) and saves it via
   `POST /api/me/wallet` — no signing, no gas, since the platform wallet does
   the minting. Anyone who hasn't linked a wallet still gets an off-chain
   token instead, so the exchange flow never breaks over a missing wallet.

5. **What happens on confirmation.** In `agree_session()` in `main.py`, once
   both peers agree: for each participant with a linked wallet, the backend
   calls `mint_onchain_token()`, which signs and sends a real
   `mintToken(...)` transaction, waits for the receipt, and parses the
   `Transfer` event for the new token ID. The tx hash and a block-explorer
   link are stored on the session and shown in the "Appointment Created!"
   popup, on `profile.html`, and on the PDF receipt. If the chain call fails
   for any reason, the error is recorded but the off-chain token still gets
   created — a flaky RPC never blocks a confirmed exchange.
