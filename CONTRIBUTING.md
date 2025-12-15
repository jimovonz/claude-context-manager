# Contributing to Claude Context Manager

Thanks for your interest in contributing!

## How to Contribute

### Reporting Issues

- Check existing issues before opening a new one
- Include your Python version, OS, and Claude Code version
- Provide steps to reproduce the issue

### Pull Requests

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Run tests: `python3 tests/test_hooks.py && python3 tests/test_purge.py`
5. Commit with a clear message
6. Push and open a pull request

### Code Style

- Follow existing code patterns
- Use type hints where practical
- Keep functions focused and documented
- Test new functionality

### Testing

Run the test suite before submitting:

```bash
python3 tests/test_hooks.py
python3 tests/test_purge.py
```

Or with pytest:

```bash
pip install pytest
pytest tests/ -v
```

## Questions?

Open an issue for questions or discussion.
