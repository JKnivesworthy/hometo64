"""
c64_prg.py — Tokenised C64 BASIC PRG builder for HomeTo64 multi-screen dashboard.

PRG layout
──────────
  [load_addr lo][load_addr hi]   2-byte header
  [BASIC lines…]
  [0x00][0x00]                   end-of-program sentinel

Each BASIC line
──────────────
  [next_ptr lo][next_ptr hi]     absolute address of NEXT line
  [line_no  lo][line_no  hi]     16-bit line number
  [token/char bytes…]            tokenised keywords + PETSCII literals
  [0x00]                         end-of-line

CRITICAL: next_ptr must point to the next line's address INCLUDING the last
line (which points to the sentinel). Setting last line's next_ptr=0x0000 causes
BASIC to stop BEFORE executing that line.

Multi-screen architecture
─────────────────────────
  Scratch RAM at $C000 stores sensor values for ALL pages as screen codes.
  HA writes updated values there via writemem regardless of active page.
  BASIC reads from scratch RAM when drawing each page — navigation is instant.

  NAV_ADDR = $CFFF (53247) — 1 byte written by HA to request a page change.
  BASIC polls PEEK(53247) on every loop iteration.

  Program structure:
    10    REM HOMETO64
    20    PRINT CLR, POKE border/bg black
    30    P=0 : POKE 53247,0       ← init page var and nav byte
    40    GOSUB 1000+P*1000        ← draw current page
    50    GET K$                   ← poll keyboard
    60    IF K$=CHR$(29) THEN ...  ← right arrow → next page
    70    IF K$=CHR$(157) THEN ... ← left arrow → prev page
    75    IF PEEK(53247)<>P THEN   ← HA navigation
    80    GOTO 50                  ← spin

    1000  REM PAGE 0 DRAW SUBROUTINE
    1010  PRINT heading
    1020  PRINT blank
    1030… PRINT sensor name rows (names only, values from scratch RAM)
    1090  RETURN

    2000  REM PAGE 1
    ...   RETURN

    (each page section: lines N000–N090, RETURN at end)
"""

from __future__ import annotations

import struct

from .const import (
    C64_BASIC_LOAD_ADDRESS,
    C64_TOKEN_PRINT,
    C64_TOKEN_GOTO,
    C64_TOKEN_GOSUB,
    C64_TOKEN_RETURN,
    C64_TOKEN_FOR,
    C64_TOKEN_TO,
    C64_TOKEN_NEXT,
    C64_TOKEN_REM,
    C64_TOKEN_END,
    C64_TOKEN_POKE,
    C64_TOKEN_PEEK,
    C64_TOKEN_GET,
    C64_TOKEN_IF,
    C64_TOKEN_THEN,
    C64_TOKEN_INT,
    PETSCII_CLEAR,
    PETSCII_REVERSE_ON,
    PETSCII_REVERSE_OFF,
    C64_COLS,
)

# ---------------------------------------------------------------------------
# Scratch RAM layout
# ---------------------------------------------------------------------------
NAV_ADDR        = 53247   # $CFFF — HA writes page index here to nav from UI
AUTO_CYCLE_ADDR = 53243   # $CFCB — HA writes jiffy interval (0=off) for auto-cycle
ACTIVITY_ADDR   = 53246   # $CFCE — C64 writes 1 on any key press; HA reads+clears+resets timer
ACTION_ADDR     = 53244   # $CFFC — C64 writes slot+1 here on Return press
BITMASK_ADDR    = 53245   # $CFFD — HA writes controllable bitmask for current page
SCRATCH_BASE    = 0xC000  # $C000 — sensor value table base
SENSORS_PER_PAGE = 10     # max sensors per page
VALUE_WIDTH     = 20      # screen codes per value

def scratch_addr(page: int, sensor_index: int) -> int:
    """Scratch RAM address for sensor value on a given page."""
    return SCRATCH_BASE + page * SENSORS_PER_PAGE * VALUE_WIDTH + sensor_index * VALUE_WIDTH

# ---------------------------------------------------------------------------
# PETSCII helpers
# ---------------------------------------------------------------------------

def _petscii(text: str) -> bytes:
    out = bytearray()
    for ch in text.upper():
        c = ord(ch)
        out.append(c if 0x20 <= c <= 0x5F else 0x3F)
    return bytes(out)

# ---------------------------------------------------------------------------
# BasicLine
# ---------------------------------------------------------------------------

class BasicLine:
    __slots__ = ("line_number", "payload")
    def __init__(self, n: int, p: bytes) -> None:
        self.line_number = n
        self.payload = p

# ---------------------------------------------------------------------------
# Line builders
# ---------------------------------------------------------------------------

def _rem(n: int, text: str = "") -> BasicLine:
    return BasicLine(n, bytes([C64_TOKEN_REM]) + (b" " + _petscii(text) if text else b""))

def _clr(n: int) -> BasicLine:
    return BasicLine(n, bytes([C64_TOKEN_PRINT, 0x22, PETSCII_CLEAR, 0x22]))

def _poke(n: int, addr: int, val: int) -> BasicLine:
    return BasicLine(n, bytes([C64_TOKEN_POKE]) + _petscii(f"{addr},{val}"))

def _print(n: int, text: str = "") -> BasicLine:
    if not text:
        return BasicLine(n, bytes([C64_TOKEN_PRINT, 0x22, 0x22]))
    return BasicLine(n, bytes([C64_TOKEN_PRINT, 0x22]) + _petscii(text) + b'"')

def _print_rv(n: int, text: str) -> BasicLine:
    return BasicLine(n,
        bytes([C64_TOKEN_PRINT, 0x22, PETSCII_REVERSE_ON])
        + _petscii(text)
        + bytes([PETSCII_REVERSE_OFF, 0x22])
    )

def _print_colored(n: int, petscii_color: int, text: str) -> BasicLine:
    return BasicLine(n,
        bytes([C64_TOKEN_PRINT, 0x22, petscii_color])
        + _petscii(text)
        + b'"'
    )

def _goto(n: int, target: int) -> BasicLine:
    return BasicLine(n, bytes([C64_TOKEN_GOTO]) + _petscii(str(target)))

def _gosub(n: int, target: int) -> BasicLine:
    return BasicLine(n, bytes([C64_TOKEN_GOSUB]) + _petscii(str(target)))

def _return(n: int) -> BasicLine:
    return BasicLine(n, bytes([C64_TOKEN_RETURN]))

def _get(n: int, var: str = "K$") -> BasicLine:
    """GET K$ — read one keypress without waiting."""
    return BasicLine(n, bytes([C64_TOKEN_GET]) + _petscii(var))

def _if_then(n: int, condition_bytes: bytes, then_bytes: bytes) -> BasicLine:
    """IF <condition> THEN <action>."""
    return BasicLine(n,
        bytes([C64_TOKEN_IF])
        + condition_bytes
        + bytes([C64_TOKEN_THEN])
        + then_bytes
    )

def _truncate(text: str, width: int) -> str:
    return text.upper()[:width]

def _heading_bar(heading: str) -> str:
    return _truncate(f" {heading} ", 39).center(39)[:39]

def _footer_bar() -> str:
    return "HOME ASSISTANT TO C64".center(39)

# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------

def _assemble(lines: list[BasicLine], load_address: int = C64_BASIC_LOAD_ADDRESS) -> bytes:
    """Assemble BASIC lines with correct next-line pointers.

    CRITICAL: every line's next_ptr (including the last) must point FORWARD
    to the next position. C64 BASIC reads next_ptr BEFORE executing a line —
    a 0x0000 next_ptr stops execution without running that line.
    The sentinel 0x0000 lives AFTER the last line's data.
    """
    encoded = [
        struct.pack("<HH", 0, bl.line_number) + bl.payload + b"\x00"
        for bl in lines
    ]
    result: list[bytes] = []
    cursor = load_address
    for raw in encoded:
        next_ptr = cursor + len(raw)   # always forward, including last line
        result.append(struct.pack("<H", next_ptr) + raw[2:])
        cursor += len(raw)
    return struct.pack("<H", load_address) + b"".join(result) + b"\x00\x00"

# ---------------------------------------------------------------------------
# Page draw subroutine builder
# ---------------------------------------------------------------------------

PETSCII_BONE_WHITE = 0x9B
PETSCII_LIGHT_BLUE = 0x9A

def _page_subroutine(
    page_index: int,
    heading: str,
    sensors: list[tuple[str, str]],
) -> list[BasicLine]:
    """Build the GOSUB draw subroutine for one page.

    Layout (25 rows, 0-indexed):
      Row  0: blank (PRINT CLR emits newline)
      Row  1: heading (reverse video)
      Row  2: blank
      Row  3: sensor 0      ← double-spaced
      Row  4: blank
      Row  5: sensor 1
      Row  6: blank
      ...
      Row 24: footer        ← always anchored here via direct POKE

    Max 10 sensors (rows 3,5,7…21). Footer POKEd directly to row 24
    screen RAM so it's always at the bottom regardless of sensor count.
    """
    base      = 1000 + page_index * 1000
    n_sensors = min(len(sensors), SENSORS_PER_PAGE)

    # Screen/colour RAM addresses for footer (always row 24)
    SCREEN_BASE   = 1024
    COLOUR_BASE   = 55296
    FOOTER_ROW    = 24
    footer_screen = SCREEN_BASE + FOOTER_ROW * C64_COLS
    footer_colour = COLOUR_BASE + FOOTER_ROW * C64_COLS

    # Build footer screen codes (39 chars, centered, reverse via colour not PRINT)
    footer_text = _footer_bar()   # 39 chars centered
    def _sc(ch):
        c = ord(ch.upper())
        if 0x20 <= c <= 0x3F: return c
        if 0x40 <= c <= 0x5F: return c - 0x40
        return 0x20
    footer_sc = bytes(_sc(ch) for ch in footer_text) + bytes([_sc(' ')] * (39 - len(footer_text)))

    lines: list[BasicLine] = [
        _rem(base, f"PAGE {page_index}"),
        _clr(base + 10),
        _print_rv(base + 20, _heading_bar(heading)),
        _print(base + 30),   # blank line after heading
    ]

    ln = base + 40
    for i, (name, _) in enumerate(sensors[:SENSORS_PER_PAGE]):
        # Cols 0-18: sensor name (19 chars, no indent)
        # Col 19:    cursor/bullet area (space normally; ▶ POKEd here when selected)
        # Cols 20-38: value area (written by writemem, 19 chars)
        # Total: 19 + 1 + 19 = 39 chars — avoids double-newline
        name_str = _truncate(name, 19).ljust(19) + " "  # 19 name + 1 cursor space
        row      = name_str + " " * 19   # 20 + 19 = 39 chars total
        color    = PETSCII_BONE_WHITE if i % 2 == 0 else PETSCII_LIGHT_BLUE
        lines.append(_print_colored(ln, color, row))
        ln += 10
        lines.append(_print(ln))   # blank line between sensors
        ln += 10

    # POKE footer directly to row 24 screen RAM (bypass PRINT entirely)
    # This anchors it regardless of how many sensors there are
    footer_ln = ln

    # POKE footer screen codes — 39 bytes chunked into safe BASIC line lengths
    # Use FOR loop: FOR J=0 TO 38:POKE footer_screen+J,footer_sc(J):NEXT J
    # But we can't put a data array in BASIC easily — use individual POKEs per char
    # Instead: POKE each byte individually, grouped onto lines of ~8 POKEs
    pokes_per_line = 6  # 6 POKEs fit safely in one BASIC line
    footer_bytes = list(footer_sc[:39])

    for chunk_start in range(0, 39, pokes_per_line):
        chunk = footer_bytes[chunk_start:chunk_start + pokes_per_line]
        # Build: POKE addr,val:POKE addr+1,val1:...
        payload = bytes([C64_TOKEN_POKE])
        for j, val in enumerate(chunk):
            addr = footer_screen + chunk_start + j
            if j == 0:
                payload += _petscii(f"{addr},{val}")
            else:
                payload += b":" + bytes([C64_TOKEN_POKE]) + _petscii(f"{addr},{val}")
        lines.append(BasicLine(footer_ln, payload))
        footer_ln += 10

    # Set footer row colour to reverse-video cyan (colour 3 = cyan, high bit = reverse)
    # C64 reverse video in colour RAM: use colour 0 (black) with screen char = reverse block
    # Actually: POKE colour RAM with colour index — use light blue (14) for footer colour
    # For reverse effect we rely on PETSCII_REVERSE_ON having been printed... but we're POKEing
    # So use character 160 (reverse space = solid block) for the footer via screen RAM
    # Actually simpler: just set colour RAM to colour 1 (white) and use screen char 32+reverse
    # The cleanest: POKE screen code 0xA0 (160 = reverse space) and colour = desired
    # But we want TEXT in the footer, so: use colour 14 (light blue) — matches heading style

    colour_pokes = [14] * 39   # light blue for footer, same as heading
    for chunk_start in range(0, 39, pokes_per_line):
        chunk = colour_pokes[chunk_start:chunk_start + pokes_per_line]
        payload = bytes([C64_TOKEN_POKE])
        for j, val in enumerate(chunk):
            addr = footer_colour + chunk_start + j
            if j == 0:
                payload += _petscii(f"{addr},{val}")
            else:
                payload += b":" + bytes([C64_TOKEN_POKE]) + _petscii(f"{addr},{val}")
        lines.append(BasicLine(footer_ln, payload))
        footer_ln += 10

    ret_ln = footer_ln
    lines.append(_return(ret_ln))
    return lines


def _value_subroutine(
    page_index: int,
    sensors: list[tuple[str, str]],
) -> list[BasicLine]:
    """Build a value-only refresh subroutine (no CLR, no PRINT).

    Copies sensor values from scratch RAM directly into screen RAM value columns.
    Called periodically (every 3s via TI check) to update displayed values
    without clearing the screen.

    Subroutine base: 5000 + page_index * 1000
    """
    base   = 11000 + page_index * 1000
    n_sensors = min(len(sensors), SENSORS_PER_PAGE)

    SCREEN_BASE  = 1024
    SENSOR_START_ROW = 3   # row 3, then every 2 rows (double-spaced)

    lines: list[BasicLine] = [_rem(base, f"VALUES P{page_index}")]
    ln = base + 10

    for i in range(n_sensors):
        screen_val_addr  = SCREEN_BASE + (SENSOR_START_ROW + i * 2) * C64_COLS + 20
        scratch_val_addr = scratch_addr(page_index, i)
        # FOR J=0 TO 19:POKE screen+J,PEEK(scratch+J):NEXT J
        payload = (
            bytes([C64_TOKEN_FOR]) + _petscii("J") + bytes([0xB2]) +
            _petscii("0") + bytes([C64_TOKEN_TO]) + _petscii("19") + b":" +
            bytes([C64_TOKEN_POKE]) + _petscii(f"{screen_val_addr}") +
            bytes([0xAA]) + _petscii("J,") +
            bytes([C64_TOKEN_PEEK]) + b"(" + _petscii(f"{scratch_val_addr}") +
            bytes([0xAA]) + _petscii("J)") + b":" +
            bytes([C64_TOKEN_NEXT]) + _petscii("J")
        )
        lines.append(BasicLine(ln, payload))
        ln += 10

    lines.append(_return(ln))
    return lines


def _cursor_subroutines() -> list[BasicLine]:
    """Cursor navigation and toggle subroutines.

    9000: Cursor DOWN — advance CS to next controllable slot, draw ▶
    9100: Cursor UP   — retreat CS to prev controllable slot, draw ▶
    9200: Return      — POKE CS+1 to ACTION_ADDR (HA reads and toggles)

    CS = current cursor slot (0-9)
    BM = PEEK(BITMASK_ADDR) — bitmask of controllable slots on current page
    ▶  = PETSCII 0x1E (screen code 30, right-pointing triangle)
    Screen address of col 0 of sensor row I = 1024 + (3 + I*2) * 40
    """
    CURSOR_CHAR = 81    # ● filled circle (toggle indicator)
    SPACE       = 32    # space to erase
    # 1024 + (3+CS*2)*40 = 1024 + 120 + CS*80 = 1144 + CS*80
    # But BASIC can't easily do CS*80 — use CS+CS (=CS*2) for row, then *40
    # Expression: 1024+(3+CS+CS)*40  which BASIC handles fine

    def b(t): return _petscii(t)

    lines: list[BasicLine] = [
        # ── 21000: Cursor DOWN ────────────────────────────────────────────
        # Initialize CS to first controllable slot if not yet set (CS=255 sentinel)
        # Then advance to next controllable slot, wrapping at 9.
        # Bounds check: IF CS>9 THEN CS=0 before any POKE
        _rem(21000, "CSR DOWN"),
        # Cache bitmap value
        BasicLine(21001, b("BM") + bytes([0xB2]) +
            bytes([C64_TOKEN_PEEK]) + b(f"({BITMASK_ADDR})")),
        # Safety: clamp CS to 0-9 before erasing
        BasicLine(21005,
            bytes([C64_TOKEN_IF]) + b("CS") + bytes([0xB1]) + b("9") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("0")
        ),
        BasicLine(21006,
            bytes([C64_TOKEN_IF]) + b("CS") + bytes([0xB3]) + b("0") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("0")
        ),
        # Erase old cursor (now safe — CS is 0-9)
        BasicLine(21010,
            bytes([C64_TOKEN_POKE]) + _petscii("1163") + bytes([0xAA]) + _petscii("CS") + bytes([0xAC]) + _petscii("80,32")
        ),
        # CS=CS+1: IF CS>9 THEN CS=0
        BasicLine(21020, b("CS") + bytes([0xB2]) + b("CS") + bytes([0xAA]) + b("1")),
        BasicLine(21030,
            bytes([C64_TOKEN_IF]) + b("CS") + bytes([0xB1]) + b("9") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("0")
        ),
        # IF bit CS of bitmask is 0 → not controllable, keep advancing
        BasicLine(21040,
            bytes([C64_TOKEN_IF]) +
            b("(BM") + bytes([0xAF]) + b("2") + bytes([0xAE]) + b("CS)") + bytes([0xB2]) + b("0") +
            bytes([C64_TOKEN_THEN]) + bytes([C64_TOKEN_GOTO]) + b("21020")
        ),
        # Draw cursor at new valid slot
        BasicLine(21050,
            bytes([C64_TOKEN_POKE]) + _petscii("1163") + bytes([0xAA]) + _petscii("CS") + bytes([0xAC]) + _petscii("80,81")
        ),
        _return(21060),

        # ── 21100: Cursor UP ──────────────────────────────────────────────
        _rem(21100, "CSR UP"),
        BasicLine(21101, b("BM") + bytes([0xB2]) +
            bytes([C64_TOKEN_PEEK]) + b(f"({BITMASK_ADDR})")),
        # Safety: clamp CS before erasing
        BasicLine(21105,
            bytes([C64_TOKEN_IF]) + b("CS") + bytes([0xB1]) + b("9") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("0")
        ),
        BasicLine(21106,
            bytes([C64_TOKEN_IF]) + b("CS") + bytes([0xB3]) + b("0") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("0")
        ),
        BasicLine(21110,
            bytes([C64_TOKEN_POKE]) + _petscii("1163") + bytes([0xAA]) + _petscii("CS") + bytes([0xAC]) + _petscii("80,32")
        ),
        BasicLine(21120, b("CS") + bytes([0xB2]) + b("CS") + bytes([0xAB]) + b("1")),
        BasicLine(21130,
            bytes([C64_TOKEN_IF]) + b("CS") + bytes([0xB3]) + b("0") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("9")
        ),
        BasicLine(21140,
            bytes([C64_TOKEN_IF]) +
            b("(BM") + bytes([0xAF]) + b("2") + bytes([0xAE]) + b("CS)") + bytes([0xB2]) + b("0") +
            bytes([C64_TOKEN_THEN]) + bytes([C64_TOKEN_GOTO]) + b("21120")
        ),
        BasicLine(21150,
            bytes([C64_TOKEN_POKE]) + _petscii("1163") + bytes([0xAA]) + _petscii("CS") + bytes([0xAC]) + _petscii("80,81")
        ),
        _return(21160),

        # ── 21200: Toggle (Return) — POKE CS+1 (range 1-10)
        _rem(21200, "TOGGLE"),
        BasicLine(21205,
            bytes([C64_TOKEN_IF]) + b("CS") + bytes([0xB1]) + b("9") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("0")
        ),
        BasicLine(21210,
            bytes([C64_TOKEN_POKE]) + b(f"{ACTION_ADDR},CS") +
            bytes([0xAA]) + b("1")
        ),
        _return(21220),

        # ── 21300: Init cursor to first controllable slot ─────────────────
        # Explicit bit checks — no loop, no 2^CS, no overflow, no infinite loop.
        # BM = cached bitmap. Checks bits 0-7 in order, draws cursor at first.
        _rem(21300, "CSR INIT"),
        BasicLine(21305, b("BM") + bytes([0xB2]) +
            bytes([C64_TOKEN_PEEK]) + b(f"({BITMASK_ADDR})")),
        BasicLine(21310,
            bytes([C64_TOKEN_IF]) + b("BM") + bytes([0xAF]) + b("1") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("0:") +
            bytes([C64_TOKEN_GOTO]) + b("21390")),
        BasicLine(21315,
            bytes([C64_TOKEN_IF]) + b("BM") + bytes([0xAF]) + b("2") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("1:") +
            bytes([C64_TOKEN_GOTO]) + b("21390")),
        BasicLine(21320,
            bytes([C64_TOKEN_IF]) + b("BM") + bytes([0xAF]) + b("4") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("2:") +
            bytes([C64_TOKEN_GOTO]) + b("21390")),
        BasicLine(21325,
            bytes([C64_TOKEN_IF]) + b("BM") + bytes([0xAF]) + b("8") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("3:") +
            bytes([C64_TOKEN_GOTO]) + b("21390")),
        BasicLine(21330,
            bytes([C64_TOKEN_IF]) + b("BM") + bytes([0xAF]) + b("16") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("4:") +
            bytes([C64_TOKEN_GOTO]) + b("21390")),
        BasicLine(21335,
            bytes([C64_TOKEN_IF]) + b("BM") + bytes([0xAF]) + b("32") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("5:") +
            bytes([C64_TOKEN_GOTO]) + b("21390")),
        BasicLine(21340,
            bytes([C64_TOKEN_IF]) + b("BM") + bytes([0xAF]) + b("64") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("6:") +
            bytes([C64_TOKEN_GOTO]) + b("21390")),
        BasicLine(21345,
            bytes([C64_TOKEN_IF]) + b("BM") + bytes([0xAF]) + b("128") +
            bytes([C64_TOKEN_THEN]) + b("CS") + bytes([0xB2]) + b("7:") +
            bytes([C64_TOKEN_GOTO]) + b("21390")),
        _return(21350),   # no controllable slot found
        BasicLine(21390,
            bytes([C64_TOKEN_POKE]) + _petscii("1163") + bytes([0xAA]) + _petscii("CS") + bytes([0xAC]) + _petscii("80,81")),
        _return(21395),
    ]
    return lines


def _build_splash() -> list[BasicLine]:
    """One-screen splash: header + house + HOME (5 rows) + TO 64 (5 rows).
    Fits in 24 PRINT rows. Row 24 = footer via POKE. Hold 5 seconds.
    Colors: BONE_WHITE + LIGHT_BLUE only.
    """
    def b(text): return _petscii(text)
    def _c(text, width=39): return text.center(width)[:39]

    BONE_WHITE = 0x9B
    LIGHT_BLUE = 0x9A
    lines: list[BasicLine] = [
        _rem(30000, "HOMETO64 SPLASH"),
        _clr(30010),
        _poke(30020, 53280, 0),
        _poke(30030, 53281, 0),
        # Rows 1-12: house from ASCII art (starts immediately after CLR)
        _print_colored(30040, BONE_WHITE, _c('********************************')),
        _print_colored(30050, BONE_WHITE, _c('*             **               *')),
        _print_colored(30060, BONE_WHITE, _c('*          ********            *')),
        _print_colored(30070, BONE_WHITE, _c('*        *************         *')),
        _print_colored(30080, LIGHT_BLUE, _c('*      *****************       *')),
        _print_colored(30090, LIGHT_BLUE, _c('*    ********************      *')),
        _print_colored(30100, LIGHT_BLUE, _c('*   ***********************    *')),
        _print_colored(30110, LIGHT_BLUE, _c('*  *************************   *')),
        _print_colored(30120, BONE_WHITE, _c('*     *******************      *')),
        _print_colored(30130, BONE_WHITE, _c('*     *******************      *')),
        _print_colored(30140, BONE_WHITE, _c('***** ******************* ******')),
        # Row 14: blank separator
        _print(30160),
        # Rows 15-19: HOME in big block letters (bone white)
        _print_colored(30170, BONE_WHITE, _c('*   * ***** *   * *****')),
        _print_colored(30180, BONE_WHITE, _c('*   * *   * ** ** *    ')),
        _print_colored(30190, BONE_WHITE, _c('***** *   * * * * ***  ')),
        _print_colored(30200, BONE_WHITE, _c('*   * *   * *   * *    ')),
        _print_colored(30210, BONE_WHITE, _c('*   * ***** *   * *****')),
        # Blank between HOME and TO 64
        _print(30220),
        # TO 64 in big block letters (light blue)
        _print_colored(30230, LIGHT_BLUE, _c('***** *****      ***  *   *')),
        _print_colored(30240, LIGHT_BLUE, _c('  *   *   *     *     *   *')),
        _print_colored(30250, LIGHT_BLUE, _c('  *   *   *     ****  *****')),
        _print_colored(30260, LIGHT_BLUE, _c('  *   *   *     *   *     *')),
        _print_colored(30270, LIGHT_BLUE, _c('  *   *****      ***      *')),
    ]

    # 5 second hold (300 jiffies) — no footer during splash
    ln = 30300
    lines.append(BasicLine(ln, b("T")+bytes([0xB2])+b("TI"))); ln += 1
    spin = ln
    lines.append(BasicLine(spin,
        bytes([C64_TOKEN_IF])+b("TI")+bytes([0xAB])+b("T")+
        bytes([0xB3])+b("300")+
        bytes([C64_TOKEN_THEN])+bytes([C64_TOKEN_GOTO])+_petscii(str(spin))
    ))
    ln += 1
    lines.append(_return(ln))
    return lines


def build_multipage_prg(pages: list[tuple[str, list[tuple[str, str]]]]) -> bytes:
    """Build a multi-page sensor dashboard PRG.

    BASIC control flow
    ──────────────────
      10   REM HOMETO64
      20   POKE border black
      25   POKE bg black
      30   P=0:N=<n_pages>:NP=0:POKE NAV_ADDR,0:T0=TI
      40   ON P+1 GOSUB 1000,2000,3000   ← draw current page
      50   GET K$
      60   IF K$=CHR$(29)  THEN P=P+1:IF P>N-1 THEN P=0   ← right
      64   IF K$=CHR$(29)  THEN NP=P:GOTO 40
      70   IF K$=CHR$(157) THEN P=P-1:IF P<0 THEN P=N-1   ← left
      74   IF K$=CHR$(157) THEN NP=P:GOTO 40
      75   IF PEEK(NAV)<NP THEN NP=PEEK(NAV):P=NP:GOTO 40  ← HA nav
      76   IF PEEK(NAV)>NP THEN NP=PEEK(NAV):P=NP:GOTO 40
      80   IF TI-T0>180 THEN T0=TI:ON P+1 GOSUB 5000,6000,7000  ← value refresh
      85   IF TI<T0 THEN T0=TI   ← handle TI clock wrap at 24h
      90   GOTO 50

    NP (nav pointer) tracks the last committed page value so keyboard nav
    doesn't get immediately overridden by the NAV_ADDR poll.

    Value refresh subroutines at 5000,6000,7000 copy scratch RAM → screen RAM
    every 3 seconds (180 jiffies) without clearing the screen.
    """
    n_pages = len(pages)
    if n_pages == 0:
        return build_hello_world_prg("HOME ASSISTANT")

    # Targets for ON..GOSUB
    draw_targets  = ",".join(str(1000 + i * 1000) for i in range(n_pages))
    # ── Control section ──────────────────────────────────────────────────
    def b(text): return _petscii(text)

    ctrl: list[BasicLine] = [
        _rem(10, "HOMETO64"),
        _poke(20, 53280, 0),
        _poke(25, 53281, 0),
        # P=0:N=<pages>:NP=0:CS=0:POKE NAV_ADDR,0:POKE BITMASK_ADDR,0:T0=TI
        BasicLine(30,
            b("P") + bytes([0xB2, 0x30, 0x3A]) +
            b("N") + bytes([0xB2]) + b(str(n_pages)) + bytes([0x3A]) +
            b("NP") + bytes([0xB2, 0x30, 0x3A]) +
            b("CS") + bytes([0xB2, 0x30, 0x3A]) +
            bytes([C64_TOKEN_POKE]) + b(f"{NAV_ADDR},0") + bytes([0x3A]) +
            bytes([C64_TOKEN_POKE]) + b(f"{BITMASK_ADDR},0") + bytes([0x3A]) +
            b("CX") + bytes([0xB2]) + b("0")   # CX = cursor drawn flag
            # (T0/TA removed — dead since auto-cycle moved to HA side)
        ),
        # Precompute key-code strings ONCE. Comparing K$ to a variable
        # allocates nothing; evaluating CHR$(n) in the loop allocates a
        # temp string EVERY iteration -> BASIC V2 GC freezes eventually.
        BasicLine(35,
            b("K1$") + bytes([0xB2, 0xC7]) + b("(29)") + bytes([0x3A]) +
            b("K2$") + bytes([0xB2, 0xC7]) + b("(157)") + bytes([0x3A]) +
            b("K3$") + bytes([0xB2, 0xC7]) + b("(17)") + bytes([0x3A]) +
            b("K4$") + bytes([0xB2, 0xC7]) + b("(145)") + bytes([0x3A]) +
            b("K5$") + bytes([0xB2, 0xC7]) + b("(13)")
        ),
        # ON P+1 GOSUB draw_targets
        BasicLine(40,
            bytes([0x91]) + b("P") + bytes([0xAA]) + b("1") +
            bytes([C64_TOKEN_GOSUB]) + b(draw_targets)
        ),
        # GET K$
        _get(50, "K$"),
        # Fast path: no key pressed -> skip the 4 page-key compares
        BasicLine(55,
            bytes([C64_TOKEN_IF]) + b("K$") + bytes([0xB2]) + b('""') +
            bytes([C64_TOKEN_THEN]) + bytes([C64_TOKEN_GOTO]) + b("75")
        ),
        # IF K$=K1$ THEN P=P+1:IF P>N-1 THEN P=0   (right)
        BasicLine(60,
            bytes([C64_TOKEN_IF]) +
            b("K$") + bytes([0xB2]) + b("K1$") +
            bytes([C64_TOKEN_THEN]) +
            b("P") + bytes([0xB2]) + b("P") + bytes([0xAA]) + b("1") + bytes([0x3A]) +
            bytes([C64_TOKEN_IF]) +
            b("P") + bytes([0xB1]) + b("N") + bytes([0xAB]) + b("1") +
            bytes([C64_TOKEN_THEN]) + b("P") + bytes([0xB2]) + b("0")
        ),
        # IF K$=CHR$(29) THEN NP=P:POKE NAV_ADDR,P:POKE BITMASK_ADDR,0:GOTO 40
        # C64 clears bitmap itself so stale bits don't trigger cursor init on new page
        BasicLine(64,
            bytes([C64_TOKEN_IF]) +
            b("K$") + bytes([0xB2]) + b("K1$") +
            bytes([C64_TOKEN_THEN]) +
            b("NP") + bytes([0xB2]) + b("P") + bytes([0x3A]) +
            bytes([C64_TOKEN_POKE]) + b(f"{NAV_ADDR},") + b("P") + bytes([0x3A]) +
            bytes([C64_TOKEN_POKE]) + b(f"{BITMASK_ADDR},0") + bytes([0x3A]) +
                        bytes([C64_TOKEN_POKE]) + b(f"{ACTIVITY_ADDR},(") +
            bytes([C64_TOKEN_PEEK]) + b(f"({ACTIVITY_ADDR})") +
            bytes([0xAA]) + b("1)") + bytes([0xAF]) + b("1") + bytes([0x3A]) +
            b("CX") + bytes([0xB2]) + b("0") + bytes([0x3A]) +
            bytes([C64_TOKEN_GOTO]) + b("40")
        ),
        # IF K$=CHR$(157) THEN P=P-1:IF P<0 THEN P=N-1  (left)
        BasicLine(70,
            bytes([C64_TOKEN_IF]) +
            b("K$") + bytes([0xB2]) + b("K2$") +
            bytes([C64_TOKEN_THEN]) +
            b("P") + bytes([0xB2]) + b("P") + bytes([0xAB]) + b("1") + bytes([0x3A]) +
            bytes([C64_TOKEN_IF]) +
            b("P") + bytes([0xB3]) + b("0") +
            bytes([C64_TOKEN_THEN]) + b("P") + bytes([0xB2]) + b("N") + bytes([0xAB]) + b("1")
        ),
        # IF K$=CHR$(157) THEN NP=P:POKE NAV_ADDR,P:POKE BITMASK_ADDR,0:GOTO 40
        BasicLine(74,
            bytes([C64_TOKEN_IF]) +
            b("K$") + bytes([0xB2]) + b("K2$") +
            bytes([C64_TOKEN_THEN]) +
            b("NP") + bytes([0xB2]) + b("P") + bytes([0x3A]) +
            bytes([C64_TOKEN_POKE]) + b(f"{NAV_ADDR},") + b("P") + bytes([0x3A]) +
            bytes([C64_TOKEN_POKE]) + b(f"{BITMASK_ADDR},0") + bytes([0x3A]) +
                        bytes([C64_TOKEN_POKE]) + b(f"{ACTIVITY_ADDR},(") +
            bytes([C64_TOKEN_PEEK]) + b(f"({ACTIVITY_ADDR})") +
            bytes([0xAA]) + b("1)") + bytes([0xAF]) + b("1") + bytes([0x3A]) +
            b("CX") + bytes([0xB2]) + b("0") + bytes([0x3A]) +
            bytes([C64_TOKEN_GOTO]) + b("40")
        ),
        # HA-driven nav: one PEEK, one compare (was two lines / up to 4 PEEKs).
        # Q=PEEK(NAV):IF Q<>NP THEN NP=Q:P=Q:POKE BITMASK,0:CX=0:GOTO 40
        # CX=0 added — BUG FIX: HA nav (Next Page / auto-cycle) previously
        # never reset CX, so the cursor could fail to init on the new page.
        BasicLine(75,
            b("Q") + bytes([0xB2]) +
            bytes([C64_TOKEN_PEEK]) + b(f"({NAV_ADDR})") + bytes([0x3A]) +
            bytes([C64_TOKEN_IF]) +
            b("Q") + bytes([0xB3, 0xB1]) + b("NP") +      # <> = 0xB3 0xB1
            bytes([C64_TOKEN_THEN]) +
            b("NP") + bytes([0xB2]) + b("Q") + bytes([0x3A]) +
            b("P") + bytes([0xB2]) + b("Q") + bytes([0x3A]) +
            bytes([C64_TOKEN_POKE]) + b(f"{BITMASK_ADDR},0") + bytes([0x3A]) +
            b("CX") + bytes([0xB2]) + b("0") + bytes([0x3A]) +
            bytes([C64_TOKEN_GOTO]) + b("40")
        ),
        # Lines 83-84: init cursor once when bitmap becomes non-zero
        # 83: IF bitmap=0 THEN skip (no controllable entities)
        # 83+1: IF CX=0 (not yet drawn) THEN draw cursor at first controllable slot
        BasicLine(83,
            bytes([C64_TOKEN_IF]) +
            bytes([C64_TOKEN_PEEK]) + b(f"({BITMASK_ADDR})") +
            bytes([0xB2]) + b("0") +
            bytes([C64_TOKEN_THEN]) + bytes([C64_TOKEN_GOTO]) + b("85")
        ),
        BasicLine(84,
            bytes([C64_TOKEN_IF]) + b("CX") + bytes([0xB2]) + b("0") +
            bytes([C64_TOKEN_THEN]) +
            b("CX") + bytes([0xB2]) + b("1") + bytes([0x3A]) +
            bytes([C64_TOKEN_GOSUB]) + b("21300")
        ),
        # Skip cursor key handling if no controllable slots
        # Down cursor
        BasicLine(85,
            bytes([C64_TOKEN_IF]) +
            b("K$") + bytes([0xB2]) + b("K3$") +
            bytes([C64_TOKEN_THEN]) +
                        bytes([C64_TOKEN_POKE]) + b(f"{ACTIVITY_ADDR},(") +
            bytes([C64_TOKEN_PEEK]) + b(f"({ACTIVITY_ADDR})") +
            bytes([0xAA]) + b("1)") + bytes([0xAF]) + b("1") + bytes([0x3A]) +
            bytes([C64_TOKEN_GOSUB]) + b("21000")
        ),
        # Up cursor
        BasicLine(86,
            bytes([C64_TOKEN_IF]) +
            b("K$") + bytes([0xB2]) + b("K4$") +
            bytes([C64_TOKEN_THEN]) +
                        bytes([C64_TOKEN_POKE]) + b(f"{ACTIVITY_ADDR},(") +
            bytes([C64_TOKEN_PEEK]) + b(f"({ACTIVITY_ADDR})") +
            bytes([0xAA]) + b("1)") + bytes([0xAF]) + b("1") + bytes([0x3A]) +
            bytes([C64_TOKEN_GOSUB]) + b("21100")
        ),
        # Return (CHR$(13)) → toggle selected entity
        BasicLine(87,
            bytes([C64_TOKEN_IF]) +
            b("K$") + bytes([0xB2]) + b("K5$") +
            bytes([C64_TOKEN_THEN]) +
                        bytes([C64_TOKEN_POKE]) + b(f"{ACTIVITY_ADDR},(") +
            bytes([C64_TOKEN_PEEK]) + b(f"({ACTIVITY_ADDR})") +
            bytes([0xAA]) + b("1)") + bytes([0xAF]) + b("1") + bytes([0x3A]) +
            bytes([C64_TOKEN_GOSUB]) + b("21200")
        ),
        _goto(88, 50),
    ]

    # ── Page draw subroutines (1000, 2000, ...) ──────────────────────────
    page_lines: list[BasicLine] = []

    for i, (heading, sensors) in enumerate(pages):
        page_lines.extend(_page_subroutine(i, heading, sensors))

    # ── Cursor subroutines (9000+) — must come after page subs numerically
    # but wait: 9000 > 1000..10000 so cursor subs must come after page subs
    # but BEFORE value subs at 11000+
    # Final order: ctrl(10-90) + pages(1000-10000) + cursor(9000-9220) + values(11000+)
    # Problem: 9000 is between 1000 and 11000 so insert cursor between pages and values
    page_lines.extend(_cursor_subroutines())

    # Line 1: GOSUB splash subroutine (splash draws screen then RETURNs)
    splash_call = [BasicLine(1, bytes([C64_TOKEN_GOSUB]) + _petscii("30000"))]
    splash_sub  = _build_splash()
    return _assemble(splash_call + ctrl + page_lines + splash_sub)


def build_sensor_prg(heading: str, sensors: list[tuple[str, str]]) -> bytes:
    """Single-page wrapper — builds a one-page multi-screen PRG."""
    return build_multipage_prg([(heading, sensors)])


def build_hello_world_prg(heading: str) -> bytes:
    """Placeholder PRG shown when no sensors are configured."""
    header = _heading_bar(heading)
    footer = _footer_bar()
    lines = [
        _rem(10, "HOMETO64"),
        _clr(20),
        _poke(30, 53280, 0),
        _poke(40, 53281, 0),
        _print_rv(50, header),
        _print(60),
        _print_colored(70,  PETSCII_BONE_WHITE, "  WAITING FOR SENSOR DATA..."),
        _print(80),
        _print_colored(90,  PETSCII_LIGHT_BLUE, "  CONFIGURE SENSORS IN THE"),
        _print_colored(100, PETSCII_LIGHT_BLUE, "  HOME ASSISTANT INTEGRATION."),
        _print(110),
        _print_colored(120, PETSCII_BONE_WHITE, "  HOMETO64"),
        _print(130),
        _print_rv(140, footer),
        _goto(150, 150),   # spin
    ]
    return _assemble(lines)


# ---------------------------------------------------------------------------
# Control RAM addresses
# ---------------------------------------------------------------------------
ACTION_ADDR  = 53244   # $CFCC — C64 writes slot+1 here when Return pressed (0=idle)
BITMAP_ADDR  = 53245   # $CFCD — HA writes controllable bitmap per page (bit N = slot N)
OPTIM_ADDR   = 53246   # $CFCE — HA writes optimistic screen-code bytes here for C64 to POKE
