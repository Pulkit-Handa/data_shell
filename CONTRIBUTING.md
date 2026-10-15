# Contributing to Data Shell

Thank you for your interest in contributing to Data Shell! This document provides guidelines for contributing to the project.

## Code of Conduct

By participating in this project, you agree to abide by our Code of Conduct.

## How to Contribute

### Reporting Issues

- Check existing issues before opening a new one
- Include clear steps to reproduce the bug
- Include relevant logs, error messages, and environment details
- Use issue templates when available

### Suggesting Features

- Open an issue describing the feature and its use case
- Explain why it would benefit the project and users
- Consider backward compatibility and the single-replica production constraint

### Pull Requests

1. Fork the repository
2. Create a feature branch from `main`: `git checkout -b feature/your-feature-name`
3. Make your changes following the style guidelines
4. Add tests for new functionality
5. Run the full test suite: `pytest -q`
6. Ensure all tests pass and code compiles: `python -m compileall datashell tests`
7. Update documentation if needed
8. Submit a pull request with a clear description

## Development Setup

```bash
# Clone the repository
git clone https://github.com/Pulkit-Handa/data_shell.git
cd data-shell

# Install in development mode with dev dependencies
pip install -e ".[dev]"

# Run tests
ENVIRONMENT=test pytest -q

# Run the demo
datashell demo
```

## Style Guidelines

- Follow PEP 8
- Use type hints where appropriate
- Keep functions focused and well-documented
- Prefer explicit over implicit
- Maintain consistency with existing codebase patterns

## Testing

- All new code must include tests
- Maintain or improve test coverage
- Tests must be deterministic and not depend on external services
- Use `tmp_path` for filesystem operations in tests
- Run the full suite before submitting: `ENVIRONMENT=test pytest -q`

## Documentation

- Update README.md if adding user-facing features
- Update relevant docs in `docs/` for architectural/operational changes
- Include docstrings for public APIs
- Add examples where helpful

## Commit Messages

Use clear, descriptive commit messages:

- Use present tense ("Add feature" not "Added feature")
- Start with a capital letter
- Keep the first line under 50 characters
- Reference issues when relevant

Thank you for contributing!
