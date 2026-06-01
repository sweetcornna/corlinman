# echo-plugin

A minimal example plugin used to seed and smoke-test the corlinman
marketplace plugin install path.

## What it does

Reads one line from stdin and writes it back, prefixed with `echo: `.

```sh
echo hi | python echo.py
# -> echo: hi
```

## Files

- `manifest.json` — plugin metadata (`name`, `version`, `entry`, `requires`).
- `echo.py` — the entry point named by `manifest.json:entry`.

The packaged tarball wraps this directory as `echo-plugin/...` so an
extractor expecting `<slug>/manifest.json` finds it at the top level.
