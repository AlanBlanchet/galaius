# Manual Desktop Testing

Test interact tools against the GTK test window.

## Setup

```bash
uv run python tests/fixtures/test_gui.py &
```

## Test Scenarios (ref-based, no raw coordinates)

### 1. Detect elements

```
get_interactive_elements(window="Interact Test", debug_dir="out/")
```

Verify: buttons (Click Me, Toggle, Reset, +, -), entry, switch, drag source, drop target all detected.

### 2. Click button via ref

```
run_actions(window="Interact Test", actions=[
  {"type": "click", "ref": "e<N>"}  # ref for "Click Me" button
])
```

Verify: status shows "Clicked: Click Me". Use `observe` to confirm.

### 3. Type text via ref

```
run_actions(window="Interact Test", actions=[
  {"type": "click", "ref": "e<N>"},        # click entry field
  {"type": "type_text", "text": "hello"},
  {"type": "screenshot", "query": "What does the echo label show?"}
])
```

Verify: echo label shows "hello".

### 4. Toggle switch via ref

```
run_actions(window="Interact Test", actions=[
  {"type": "click", "ref": "e<N>"}  # ref for switch
], query="What is the switch status?")
```

Verify: switch_status label shows "ON".

### 5. Counter increment via ref

```
run_actions(window="Interact Test", actions=[
  {"type": "click", "ref": "e<N>"},  # ref for "+"
  {"type": "click", "ref": "e<N>"},  # click again
  {"type": "click", "ref": "e<N>"},  # and again
], query="What is the counter value?")
```

Verify: counter shows "Count: 3".

### 6. Drag & Drop via refs

```
run_actions(window="Interact Test", actions=[
  {"type": "drag", "from_ref": "e<N>", "to_ref": "e<N>"}  # drag source → drop target
], query="What does the drop zone say?")
```

Verify: drop label shows "Dropped!", background turns green, status shows "Drop received!".

### 7. Full sequence

Detect → click "+" 3x → type "test" → drag → observe → click "Reset" → observe reset.

## Debug output

All tool calls with `debug_dir="out/"` produce:

- `window_geometry.json` — xwininfo vs screenshot dimensions + offsets
- `input_screenshot.png` — raw capture
- `annotated.png` — bounding boxes overlay
- `vlm_raw.txt` — model response (if VLM used)
- `vlm_meta.json` — model, timing, coord_format, dimensions
  - output.txt (final result)

## What to check

- Bounding boxes match actual UI elements (view annotated.png)
- Click lands on intended element (cursor type confirms)
- Errors are clear when element not found
- No stale element cache causing misclicks
