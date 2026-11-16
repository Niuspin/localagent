# LocalAgent Offline Web Fonts (`web/fonts/`)

By default, LocalAgent uses calibrated system UI, editorial serif, and coding monospace stacks (`Segoe UI Variable` / `PingFang SC` / `Microsoft YaHei UI` / `Cascadia Code`) so first-screen payload stays lightweight and works 100% offline under strict CSP (`font-src 'self'`).

If you drop optional open-source webfont subsets into this directory:
- `Inter-Variable.woff2` (SIL OFL 1.1)
- `JetBrainsMono.woff2` (SIL OFL 1.1)

Register matching `@font-face` entries in `web/css/tokens.css` only when those files are present, and keep `THIRD_PARTY_LICENSES.md` synchronized.
