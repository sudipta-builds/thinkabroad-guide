# Guide

WhatsApp-only personal AI assistant that finds (and later applies to) jobs — including visa-sponsored roles.

No web UI. No ThinkAbroad linkage. Users only text Guide.

## Conversation

1. Guide: *Hello — I'm Guide, your personal AI assistant to help you find jobs. Tell me which country you're interested in.*
2. User types a country (free text — not a menu).
3. Guide asks for the kind of job; user writes it in their own words.
4. Guide **searches the live web** (no seeded job list), returns matches.
5. User: `APPLY 1` → cover draft → `YES` to confirm.
6. Optional: `SAVE LINKEDIN email password` stores encrypted logins in Guide's **vault** for future browser apply.

## LLM choice

| Model | Role |
|--------|------|
| **Gemini** (+ Google Search grounding) | Best for *finding* real jobs on the web |
| **DeepSeek** | Best cheap chat + CV / cover drafts |
| **Brave Search** | Fallback search if Gemini grounding isn't available |

Set both Gemini + DeepSeek when possible. Guide will use each where it's strongest.

## Screenshots + links

After a live search, Guide sends **job links in text** and **page screenshots** (top results) on WhatsApp so you can review before `APPLY N`. On `APPLY N` it sends another screenshot of that listing plus the cover draft, then waits for `YES`.

## Cloud — do you need a server?

**Yes.** Twilio must POST inbound WhatsApp messages to a public HTTPS URL that is always on. See [DEPLOY.md](DEPLOY.md).

| Need | Why |
|------|-----|
| **Always-on cloud API** (e.g. Render Starter) | WhatsApp webhook + Guide brain + SQLite + vault |
| **Persistent disk** | Survive restarts (DB + vault ciphertext) |
| **Cloud browser later** (Browserbase / similar) | Log into LinkedIn/ATS with vault credentials and submit applies |

Render is enough for the brain + vault + web search. You do **not** need a separate VPS for v1. You **will** need a cloud browser service when auto-apply goes live.

## Twilio

Reuse the **existing ThinkAbroad Twilio account** — paste the same `TWILIO_*` env vars into Guide's Render service. Point the WhatsApp webhook (or a dedicated sender number) at:

`https://YOUR-GUIDE-SERVICE.onrender.com/whatsapp`

## Env

See `.env.example`. Minimum to search jobs: `TWILIO_*` + (`GEMINI_API_KEY` or `BRAVE_API_KEY`) + preferably `DEEPSEEK_API_KEY`. For vault: `VAULT_SECRET_KEY`.

## Commands

| Message | Action |
|---------|--------|
| `HI` | Welcome + ask country |
| free text | Country → role → live search |
| `JOBS` | Search again |
| `APPLY N` | Draft cover + confirm |
| `YES` / `NO` | Confirm / cancel apply |
| `CV` | Show CV draft |
| `STATUS` | Applications |
| `VAULT` | How to save logins |
| `SAVE LINKEDIN email pass` | Encrypt login into vault |
