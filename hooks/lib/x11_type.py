#!/usr/bin/env python3
"""
X11 keystroke injection via ctypes. No external dependencies.
Uses XTest extension to send synthetic key events.
"""

import ctypes
import ctypes.util
import time

# Load X11 libraries
_x11 = ctypes.CDLL(ctypes.util.find_library('X11') or 'libX11.so.6')
_xtst = ctypes.CDLL(ctypes.util.find_library('Xtst') or 'libXtst.so.6')

# X11 types
Display = ctypes.c_void_p
KeyCode = ctypes.c_ubyte
KeySym = ctypes.c_ulong
Bool = ctypes.c_int
Time = ctypes.c_ulong

# Function signatures
_x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
_x11.XOpenDisplay.restype = Display
_x11.XCloseDisplay.argtypes = [Display]
_x11.XFlush.argtypes = [Display]
_x11.XKeysymToKeycode.argtypes = [Display, KeySym]
_x11.XKeysymToKeycode.restype = KeyCode
_x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
_x11.XStringToKeysym.restype = KeySym

_xtst.XTestFakeKeyEvent.argtypes = [Display, ctypes.c_uint, Bool, Time]
_xtst.XTestFakeKeyEvent.restype = ctypes.c_int

# Modifier keysyms
SHIFT_KEYSYM = 0xffe1  # XK_Shift_L

# Character to keysym name mapping for special chars
CHAR_MAP = {
    ' ': 'space', '\n': 'Return', '\t': 'Tab',
    '-': 'minus', '=': 'equal', '[': 'bracketleft', ']': 'bracketright',
    ';': 'semicolon', "'": 'apostrophe', '\\': 'backslash', ',': 'comma',
    '.': 'period', '/': 'slash', '`': 'grave',
    '!': 'exclam', '@': 'at', '#': 'numbersign', '$': 'dollar',
    '%': 'percent', '^': 'asciicircum', '&': 'ampersand', '*': 'asterisk',
    '(': 'parenleft', ')': 'parenright', '_': 'underscore', '+': 'plus',
    '{': 'braceleft', '}': 'braceright', '|': 'bar', ':': 'colon',
    '"': 'quotedbl', '<': 'less', '>': 'greater', '?': 'question', '~': 'asciitilde',
}

SHIFT_CHARS = set('~!@#$%^&*()_+{}|:"<>?ABCDEFGHIJKLMNOPQRSTUVWXYZ')


def type_string(text: str, delay: float = 0.01) -> bool:
    """
    Type a string by injecting X11 key events.
    Returns True on success, False on failure.
    """
    display = _x11.XOpenDisplay(None)
    if not display:
        return False

    try:
        shift_keycode = _x11.XKeysymToKeycode(display, SHIFT_KEYSYM)

        for char in text:
            needs_shift = char in SHIFT_CHARS

            # Get keysym for character
            if char in CHAR_MAP:
                keysym_name = CHAR_MAP[char]
            elif char.isalpha():
                keysym_name = char.lower()
            elif char.isdigit():
                keysym_name = char
            else:
                keysym_name = CHAR_MAP.get(char, char)

            keysym = _x11.XStringToKeysym(keysym_name.encode())
            if keysym == 0:
                continue  # Skip unknown characters

            keycode = _x11.XKeysymToKeycode(display, keysym)
            if keycode == 0:
                continue

            # Press shift if needed
            if needs_shift:
                _xtst.XTestFakeKeyEvent(display, shift_keycode, True, 0)

            # Press and release key
            _xtst.XTestFakeKeyEvent(display, keycode, True, 0)
            _xtst.XTestFakeKeyEvent(display, keycode, False, 0)

            # Release shift if needed
            if needs_shift:
                _xtst.XTestFakeKeyEvent(display, shift_keycode, False, 0)

            _x11.XFlush(display)
            time.sleep(delay)

        return True
    finally:
        _x11.XCloseDisplay(display)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        text = ' '.join(sys.argv[1:])
        print(f"Typing in 3 seconds: {text}")
        time.sleep(3)
        if type_string(text):
            print("Done")
        else:
            print("Failed - could not open display")
            sys.exit(1)
    else:
        print("Usage: x11_type.py <text to type>")
