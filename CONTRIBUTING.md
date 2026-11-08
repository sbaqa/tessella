# Contributing to Tessella

Thanks for helping. Tessella is in early development, so please open an issue before a large
change.

## Set up

```bash
python3 -m venv venv && source venv/bin/activate
pip install -e ".[all,dev]"
venv/bin/pytest
```

Tests never call a model: replace `tessella.integrations.llm._call` with a fake.

## Rules that matter

- Every failure stays explainable: a verdict cites evidence, and nothing is filed without a
  person's approval.
- Every outbound request goes through `net.client`; every model call through `integrations.llm`.
- Local models come first. Cloud models are optional and pass the data policy.
- Never commit a `tessella.yaml`: it is your own config. Change `config.py` and
  `tessella.example.yaml` instead.

Run `venv/bin/pytest` before opening a pull request. By contributing you agree your work is
licensed under Apache-2.0.
