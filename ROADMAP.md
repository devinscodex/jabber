# Roadmap

Status key: [x] done, [ ] planned. Order within a section is rough priority.

## Foundation
- [x] Config layer: `mode = solo|multi`, fail-closed validation
- [x] Local accounts (scrypt, sessions, lockout) and admin CLI
- [x] Model backends: Ollama and OpenAI-compatible, auto-detected
- [x] HTTP server: login/logout/first-run setup, HttpOnly/SameSite/Secure cookies, CSRF, CSP, streaming chat, model list (minimal UI)
- [x] Full chat interface ported: sidebar, conversations, projects (with instructions), themes, settings, markdown, regenerate, phone layout, sign-in screen
- [x] Per-user conversation and project storage, isolated by account
- [ ] Strip instance-specific prompts and tools; move to config

## Auth
- [ ] Generic OIDC provider (Keycloak, Authentik, Zitadel, Pocket ID, Entra, ...)
- [ ] Reverse-proxy provider (oauth2-proxy, Authelia and similar)
- [ ] Passkeys (WebAuthn) for local accounts
- [ ] LDAP bind provider
- [ ] Admin screen in Settings: users, roles, disable, reset

## Users
- [ ] Real user records (full name, role, department, preferences) keyed by
      email, independent of the identity provider, so the assistant knows who
      it is talking to instead of parsing a name from an address

## Files
- [x] Chunked uploads (no 50 MB ceiling), per-user quota, PDF/Word/Excel/PowerPoint extraction, optional OCR
- [x] Model tools to list, read, search and view project files
- [x] Web search and page fetch (per-chat switch, DuckDuckGo, optional SearXNG/Obscura, private-network guard)
- [x] Image tools for the model: look, crop/zoom, metadata (camera, date, GPS); viewed images stay attached on later turns
- [x] Capability-aware system prompt and a reasoning-time cap
- [x] Admin People page: let people in with a temporary password, reset, disable, promote, remove
- [ ] Resumable uploads after a dropped connection; browser local-folder connection

## Operations
- [x] Context window fitting with a visible meter; full history stays on disk
- [x] Operator event log (timings, tokens, outcomes, sign-ins; no message text) and `jabber.stats`
- [ ] Optional summary of dropped history; search tool over saved conversations
- [ ] Per-chat thinking toggle; request queue with "N ahead" and a cap

## Settings
- [ ] About: version number, commit, link to release history
- [ ] Toggle for the background grid texture
- [ ] Feedback section (below)
- [ ] Theme list cleanup

## Feedback
- [ ] Settings -> Feedback: category (bug / idea / praise), free text
- [ ] Thumbs up/down on each reply with an optional note
- [ ] Conversation text is attached only when the user opts in per submission
- [ ] Stored per instance in the data dir; admins can list and export
- [ ] Config: `feedback.enabled`, optional notify address

## Files
- [ ] Project file upload and browser, attach files to a chat (UI is hidden until the server supports it)
- [ ] Chunked, streamed upload to disk; per-user quota instead of a fixed 50 MB cap
- [ ] Local folder connection in the browser (File System Access API,
      Chromium) so files are read on demand without uploading
- [ ] Startup report of which file types can be read (pdftotext, tesseract,
      LibreOffice are optional)

## Skills
- [ ] Per-user skill folders of markdown (`SKILL.md` with name and description)
- [ ] Only name and description go in the prompt; a `read_skill` tool loads the body
- [ ] No execution of bundled scripts in v1
- Reference model: Cairn (a separate personal project) runs exactly this
  shape today -- a name+description-only index stays in context, and the
  full SKILL.md body (plus any linked reference/template/script files)
  loads only when a specific skill is actually invoked. The description
  field doing real work matters more than it sounds: Cairn's own
  convention caps it at ~60 chars and requires "Use when X. Does Y." so
  the index itself is enough to pick the right skill without ever paying
  to load bodies that don't apply.

## Agents
- [ ] Named personas, not just one undifferentiated model: a persona
      carries its own stated lane (e.g. "code correctness," "business
      value," "security"), its own tool allowlist, and can be dispatched
      for a specific task rather than always talking to the same default
      assistant.
- [ ] Multi-persona review: dispatch 3-5 personas in parallel against the
      same artifact (a diff, a design, a decision) and synthesize
      convergent vs. divergent findings, instead of one pass from one
      voice.
- Reference model: Cairn runs ~30 named personas this way today (e.g. a
  code-review lane, a security lane, a business-value lane, a version-
  control lane), each a real, separately-defined prompt/tool-scope, not a
  single model asked to "think about it from angle X." The useful part
  worth porting isn't the persona count, it's the pattern: give each
  persona a narrow, stated job and let independent convergence across
  several of them be the actual signal, not any single persona's opinion
  alone.

## Library
- [ ] A browsable library for anything a tool call produces (generated
      images, documents, spreadsheets, presentations) -- separate from
      Files (user uploads) and separate from Projects (workspaces),
      matching Hermes's own split: a flat, session/chat-linked artifact
      browser is a different surface from a project workspace, never
      merged into one page.
- [ ] Sidecar metadata per item (created-at, mime, size, originating
      chat) written at creation time, not backfilled later -- provenance
      that isn't captured up front can't be added after the fact.
- [ ] Date-grouped sections (Today / Yesterday / This week / Older),
      sort by date or name, a Download/Delete action per item, direct
      upload into the library independent of any chat or project.
- [ ] A visible "still generating" placeholder for an in-flight item,
      not just a result that appears once finished -- otherwise a user
      who navigates to the library mid-generation sees nothing and may
      assume it failed.
- [ ] A real per-upload file-type allowlist and a per-user (not only
      global) storage cap before this ships -- a direct-to-library
      upload path is lower-friction than a project-scoped one and needs
      its own abuse-resistance, not an inherited assumption.

## Governance
- [ ] A registry of deterministic safety checks that sit between the
      model and any side effect (a write, a tool call, a response the
      user sees) -- each entry states its failure class, its mechanism
      in one line, what it's been tested against, and its fail-open/
      fail-closed mode. The registry itself, kept current, matters as
      much as any individual check.
- [ ] A cheap, pre-inference classifier for requests that need real
      human/escalated judgment, run BEFORE the model sees the request --
      route away from the model rather than trust it to recognize when
      it's out of its depth.
- [ ] A declared sensitivity default for stored data (general internal
      vs. anything customer- or IP-sensitive), so a future integration
      touching more sensitive data inherits a stricter default
      automatically instead of by someone remembering to ask.
- Reference model: Cairn (a separate personal project) runs this exact
  shape for its own local models -- a registry of ~11 deterministic
  checks, evidence-dated, each with a declared fail mode; and a decision-
  gate specifically for the second item above, built after finding that
  a model which scored perfectly on an isolated judgment call failed
  almost completely the instant that same judgment had to compete against
  other real priorities in one request. The fix there was a cheap
  classifier in front of the model, not a better model -- the same shape
  applies to any place Jabber needs to pick a model/tool for a risky
  request.

## Integrations
- [ ] Microsoft 365 mail (read-only first): summarize what matters, review
      mail rules, flag suspicious messages, alert a configured security address
- [ ] Mail content goes only to local models
