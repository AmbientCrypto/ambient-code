# Releasing ambient-code

This repo is self-hosting: it is both the plugin and its own marketplace
(`.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json`, plugin source
`"./"`). Users install with:

```bash
claude plugin marketplace add AmbientCrypto/ambient-code
claude plugin install ambient-code@ambient
```

## Release steps

1. **Bump the version in all three places** (CI + tests enforce sync):
   - `.claude-plugin/plugin.json` → `version`
   - `pyproject.toml` → `version`
   - `bin/ambient` → `__version__`
2. Update `CHANGELOG.md` (new top entry).
3. Gates, all green:
   ```bash
   python3 -m unittest discover -s tests     # hermetic suite
   claude plugin validate . --strict         # manifest/marketplace validation
   bash tools/stress_test.sh                 # live battery (spends a little; optional per-release)
   ```
4. Secret scan before pushing anything public:
   ```bash
   grep -rniE 'api[_-]?key=|Bearer [A-Za-z0-9]' . --exclude-dir=.git
   # only variable names / test fixtures should match — never real values
   ```
5. Commit, tag, push:
   ```bash
   git commit -am "release: vX.Y.Z"
   git tag ambient-code--vX.Y.Z
   git push origin main --tags
   ```
6. Verify as a user:
   ```bash
   claude plugin marketplace update ambient
   claude plugin update ambient-code@ambient
   claude plugin list        # shows the new version
   ```

## The pinning rule

With `version` set in plugin.json, **users receive nothing until the field bumps**
— every user-visible change requires a version bump. This is the intended stable
channel. (A future dev channel would be a second marketplace entry pointing at a
`dev` branch whose plugin.json omits `version`.)

## Uninstall story (document in support replies)

`claude plugin uninstall ambient-code@ambient` removes the plugin only. User
data lives outside it: the OS-keychain key (`ambient setup --remove`), the chunk
cache (`ambient cache clear`), the PATH launcher (`ambient link --remove`), and
`~/.config/ambient/` (sticky settings — delete to reset).
