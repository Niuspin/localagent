## Summary of Changes
Provide a clear and concise summary of the changes introduced in this PR.

## Motivation & Context
Why is this change required? What problem does it solve? If it fixes an open issue, link it here: `Fixes #...`.

## Type of Change
- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update
- [ ] Performance / Refactoring improvement

## Verification & Testing
Describe the tests you ran to verify your changes:
- [ ] Targeted pytest suite passes: `pytest tests/test_unit.py tests/test_bootstrap.py -q -p no:cacheprovider`
- [ ] Full pytest suite passes: `pytest -q -p no:cacheprovider`
- [ ] Desktop window launch verified (native `pywebview` / fallback browser)
- [ ] Tested on target platform (Windows / Linux / macOS)

## Security & Invariant Checklist
- [ ] Offline-only UI policy preserved (no external CDNs, fonts, or tracking scripts).
- [ ] PathGuard and workspace boundary restrictions maintained.
- [ ] Single-process lifetime preserved (closing window terminates background engine).
- [ ] Backward compatibility maintained for existing tests.
