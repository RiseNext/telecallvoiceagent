# rn-providers — every external system, behind an interface

The only package permitted to import a vendor SDK. Enforced by an import-linter contract.

## Owns

| Seam | First implementation |
|---|---|
| `TelephonyProvider` | Exotel |
| `RealtimeVoiceProvider` | OpenAI Realtime |
| `STTProvider` / `TTSProvider` | Sarvam |
| `LLMProvider` | OpenAI (Sarvam is OpenAI-compatible) |
| `EmbeddingProvider` | OpenAI |
| `MessagingProvider` | Exotel WhatsApp |
| `StorageProvider` | S3-compatible |
| `IdentityProvider` | Clerk |
| `CalendarProvider` / `CRMProvider` | none yet — interface only when needed |

## Rules

- **Extras, not a fat install.** Vendor SDKs live behind optional dependencies (`rn-providers[openai]`, `[aws]`, `[clerk]`) so the voice gateway does not ship an object-storage SDK it never calls. Declare the narrowest extra that works.
- **Adapters translate, they do not decide.** No business rules, no tenant logic, no policy. An adapter turns our vocabulary into theirs and back.
- **Timeouts and retries are the adapter's job.** Every outbound call has an explicit timeout. Retries only where the operation is genuinely idempotent — never blind-retry a dial or a message send.
- **Every adapter has a fake.** The fake lives beside it and is maintained as first-class code, because most development and all of CI runs against fakes. A contract test runs the same suite against the fake and (opt-in, rarely) the real provider to keep the fake honest.

## Write the interface when the second implementation is imminent

Not before. Speculative interfaces for providers we have not chosen produce abstractions shaped by imagination rather than by two real implementations. The exceptions are the seams that protect the hot path or that we already know we will swap — telephony, realtime voice, STT/TTS — and those are worth writing now.

## Be honest about leaks

Some provider differences do not abstract cleanly: interim transcripts (one provider streams them, another emits nothing until end-of-speech), barge-in mechanics, turn-detection ownership, voice catalogues, session lifetimes. These are exposed through an explicit capabilities object that callers branch on. A uniform-looking interface that hides them produces code that fails silently on the fallback path.
