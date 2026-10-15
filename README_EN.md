# XiaoAi-LLM-Router — Huffman fork

[中文](README.md) · [Upstream](https://github.com/FelixQiu1/XiaoAi-LLM-Router)

Read XiaoAi conversation text from Xiaomi cloud, send triggered questions to an LLM, and speak the answer on the originating device. This fork retains the upstream Git history and MIT license.

Improvements include persistent login sessions, MiNA device ID handling, cloud conversation ingestion, independent device histories and deduplication, paced MIoT speech chunks, optional music stopping before speech, and provider-reported token usage records.

## Important limitations

- This is a third-party integration, not an officially authorized Xiaomi service. An open-source license does not grant permission to use Xiaomi services commercially.
- Some questions visible on the speaker screen never appear in cloud history. Tested short questions work on LX04 and L05B, but long-question ingestion is not reliable.
- Xiaomi cloud is still required when the LLM runs locally with Ollama.
- Context is device-specific, retains 10 turns by default, expires after 10 idle minutes, and is lost on restart. Every follow-up still needs a trigger word.
- Long replies are split into bounded chunks with conservative timing estimates. These are not verified playback completion callbacks. Use non-streaming mode for the tested speech path. Barge-in and robust parallel device scheduling are not implemented.
- Token records are not spending limits. The application does not enforce user budgets, rate limits or output-token caps. Missing provider usage is unknown rather than zero. Provider billing remains authoritative.

## Quick start

Use Python 3.11 or 3.12:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env
```

Fill credentials and API keys in `.env`. Then list devices from an interactive terminal, completing SMS verification if requested:

```bash
.venv/bin/python main.py --list-devices
```

Copy the **MiNA deviceID** into `mi.device_id` or `mi.devices`. This differs from the numeric MIoT DID.

```bash
.venv/bin/python main.py --config config.yaml
```

Ask a short question beginning with “小爱同学，深思” or “小爱同学，问问AI”.

See the [Chinese guide](README.md) for Docker configuration, token persistence, optional usage logging and development commands. Credentials, tokens, private deployment notes and conversation logs are excluded from source control.

## License

MIT. Original copyright FelixQiu1. Dependencies: [MiService](https://github.com/Yonsm/MiService) and [LiteLLM](https://github.com/BerriAI/litellm).
