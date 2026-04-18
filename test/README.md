# Top Timer OCR Test

Recommended top-timer region for the provided screenshot:

- Resolution: `1919x1079`
- Region: `x=1060, y=85, w=360, h=120`

Practical config for game window `1920x1080`:

- `timer_x = 1060`
- `timer_y = 85`
- `timer_width = 360`
- `timer_height = 120`

## Run test

1. Put the screenshot into clipboard (or save it as `test/input_from_clipboard.png`).
2. Save from clipboard:

```powershell
python test/extract_clipboard_image.py
```

3. Validate OCR:

```powershell
python test/check_top_timer.py
```

If all runs are successful, script prints `PASS`.
