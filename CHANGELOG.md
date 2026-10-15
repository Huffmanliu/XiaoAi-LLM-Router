# Changelog

## Unreleased

- Clarify that short spoken sentences do not limit the overall answer; explicitly ask for every requested item in enumerations.
- Save the provider's finish reason in usage records and warn when a token limit truncates output, including streams whose final chunk contains only usage.
- Keep one model request per answer; no automatic paid continuations are introduced. Prompt changes improve completeness but cannot guarantee model compliance.

## huffman-v0.1.0 — 2026-10-15

First maintained fork release, based on upstream commit `87c627c317fd00dd8dfae7a565ae83c6fcbfe0b6`.

### Device routing and speech

- Keep Xiaomi HTTP sessions alive, reuse saved authentication, and clean up owned sessions.
- Use the configured device and correct MiNA deviceID field; select the connected MiNA speaker for playback.
- Read cloud conversation history when device NLP buffers are empty.
- Poll configured devices with independent timestamp and request deduplication; ignore pre-start history and back off failing devices.
- Support environment-based device selection and load configuration before listing devices.
- Fix the stream buffer loop for short sentence fragments.
- Split long MIoT speech into bounded commands and pace submission; optionally stop music before speech.
- Use the bundled LiteLLM model-cost map to avoid startup network dependency.

### Usage records

- Save provider-reported input, output, cache and reasoning usage with device/request identifiers.
- Record failed or interrupted calls with unknown usage when no usage is returned.
- Exclude conversation text and credentials from the usage ledger.

### Maintenance

- Add regression tests without live account or device dependencies.
- Document observed limitations and remove unsupported offline/one-click promises.
- Fix missing runtime modules and directory permissions in Docker packaging.
- Validate tests, lint and container imports in CI; keep private deployment artifacts out of Git and Docker contexts.
