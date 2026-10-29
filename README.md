# Jabber

An open web chat framework for local AI models. Self-hosted, privacy-first,
and usable from any machine that runs a model server.

Status: early. The configuration layer and built-in account system exist;
the web server and model backends are being ported in.

## Design

- **Modes.** `mode = "solo"` is one person on localhost with no login.
  `mode = "multi"` is several people and refuses to start without a real
  auth provider.
- **Auth providers.** `local` (built-in accounts), `oidc` (any OpenID
  Connect issuer: Keycloak, Authentik, Zitadel, Pocket ID, Entra, ...),
  `proxy` (identity from a trusted reverse proxy).
- **Backends.** Ollama and OpenAI-compatible servers (llama.cpp, vLLM,
  LM Studio).
- **No build step.** Python standard library plus optional dependencies.

## Context and logging

The full conversation is always kept on disk. Each request sends the model
only the system text plus as many recent messages as fit in about 75% of
`context_tokens`; the page shows a context meter and says when earlier
messages are saved but outside the model's memory.

Jabber writes one JSON line per chat request and sign-in event to
`<data_dir>/logs/events.jsonl` (owner-only, rotated at 20 MB). It records
timings, token counts, model names and outcomes, never message text.

    python -m jabber.stats --days 7
    python -m jabber.stats --days 1 --raw 20

## People

The first account created in the browser is the admin. Admins get a People section
in Settings: let someone in (you get a one-time temporary password to pass on),
reset a password, disable, promote or remove. People with a temporary password must
choose their own before doing anything else.

## Files

Attach files to a chat or upload them to a project (files and whole folders,
drag and drop included). Uploads are sent in 8 MB chunks, so the per-file limit
is only a disk limit (`max_upload_bytes`, default 2 GB) and each user has a
total quota (`user_quota_bytes`, default 20 GB).

- Text and code are stored as they are. Word, Excel, PowerPoint and EPUB are read with
  the standard library only. PDFs need `pdftotext` (poppler-utils); scanned
  pages are read with OCR if `indra-ocr` (`pip install indra-ocr`, CPU only) or
  `tesseract` is installed: indra first, tesseract for pages indra fails or loops on.
  indra's code is Apache-2.0 but its model weights have their own licence
  (modified OpenRAIL-M); read it before commercial use. Set `JABBER_INDRA_BIN` if
  `indra-ocr` is not on the server's PATH. Old .doc/.xls/.ppt need
  LibreOffice. Anything that can't be read is refused with the reason.
- The model reads project files with read-only tools (list, read in pages,
  search, look at an image), scoped to that project. Turn off with `tools = false`.
- Images are checked by content and size, and pasted images go straight to the chat.

## Tests

    python -m unittest discover -s tests -t .

## Security

This project is early and open to review. If you find a security issue,
please open an issue rather than a public PR with exploit details, so it
can be looked at before details are public.

## License

MIT
