# Contributing

Bug reports and focused pull requests are welcome.

For changes, please include the behavior you tested and run:

```bash
python -m compileall -q cursor_bridge tests
pytest
```

Do not include local secrets, `state/`, `.env`, recordings with private paths, or
personal Telegram/Cursor account details in issues or pull requests.
