# ccalc

A green-screen terminal calculator with a clickable keypad, number-pad typing,
and clipboard support. Pure Python, standard library only.

## Install

    pipx install git+https://github.com/jmorales222/ccalc.git

or from a checkout: `pip install .`

## Use

    ccalc            # interactive
    ccalc "2^10+1"   # one-shot

Click the buttons or type. Enter or `=` evaluates; `ans` is the last result,
and starting a line with an operator continues from it.

| Action   | Button   | Keys            |
|----------|----------|-----------------|
| Copy     | Copy     | F2 / Ctrl-Y     |
| AutoCopy | Auto:ON  | F3 / Ctrl-T     |
| Clear    | C        | Esc             |
| Quit     |          | Ctrl-C          |

AutoCopy copies every new result to the clipboard automatically.

Clipboard backends: `wl-copy`, `xclip`, `xsel`, `pbcopy`, with an OSC 52
fallback for terminals/SSH sessions. Typed expressions also support functions
such as `sqrt`, `sin`, `log`, `factorial` and constants `pi`, `e`.
