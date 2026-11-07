"""ccalc - green-screen terminal calculator with clipboard support.

Click the buttons or type (number pad works). Enter or = evaluates.
`ans` is the last result; starting a line with an operator continues from it.
  Copy button / F2 / Ctrl-Y   copy last result to clipboard
  AutoCopy button / F3 / Ctrl-T   toggle copying every new result automatically
  Esc clears the line, Up/Down recall history, Ctrl-C quits.
"""
import ast
import base64
import curses
import math
import operator
import os
import shutil
import subprocess
import sys

# ---------- safe evaluator ----------
BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
          ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
          ast.Mod: operator.mod, ast.Pow: operator.pow,
          ast.BitAnd: operator.and_, ast.BitOr: operator.or_,
          ast.BitXor: operator.xor, ast.LShift: operator.lshift,
          ast.RShift: operator.rshift}
UNOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Invert: operator.invert}
FUNCS = {n: getattr(math, n) for n in (
    "sqrt sin cos tan asin acos atan atan2 sinh cosh tanh log log2 log10 exp "
    "floor ceil factorial gcd degrees radians hypot fabs").split()}
FUNCS.update(abs=abs, round=round, min=min, max=max, ln=math.log)
CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def evaluate(text, ans):
    text = text.replace("^", "**").replace("×", "*").replace("÷", "/")
    names = dict(CONSTS, ans=ans)

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in BINOPS:
            l, r = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(r) > 10000:
                raise ValueError("exponent too large")
            return BINOPS[type(n.op)](l, r)
        if isinstance(n, ast.UnaryOp) and type(n.op) in UNOPS:
            return UNOPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Name) and n.id in names:
            return names[n.id]
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in FUNCS and not n.keywords):
            return FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError("unsupported expression")

    return ev(ast.parse(text.strip(), mode="eval"))


def fmt(v):
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return f"{v:.12g}"
    return str(v)


# ---------- clipboard ----------
def copy_to_clipboard(s):
    """Returns the method used, or None on failure."""
    cmds = []
    if os.environ.get("WAYLAND_DISPLAY"):
        cmds.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        cmds += [["xclip", "-selection", "clipboard"], ["xsel", "-ib"]]
    cmds += [["wl-copy"], ["pbcopy"]]
    for c in cmds:
        if shutil.which(c[0]):
            try:
                # xclip/wl-copy fork to hold the selection; don't wait on pipes
                p = subprocess.Popen(c, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     start_new_session=True)
                p.stdin.write(s.encode())
                p.stdin.close()
                p.wait(timeout=2)
                if p.returncode == 0:
                    return c[0]
            except Exception:
                pass
    # OSC 52 fallback (works in many terminals, incl. over SSH)
    try:
        b = base64.b64encode(s.encode()).decode()
        with open("/dev/tty", "w") as t:
            t.write(f"\033]52;c;{b}\a")
        return "OSC52"
    except Exception:
        return None


# ---------- UI ----------
# (label, action, col, row, colspan)   action: ("ins", text) or ("cmd", name)
LAYOUT = [
    ("C", ("cmd", "clear"), 0, 0, 1), ("⌫", ("cmd", "back"), 1, 0, 1),
    ("(", ("ins", "("), 2, 0, 1), (")", ("ins", ")"), 3, 0, 1),
    ("7", ("ins", "7"), 0, 1, 1), ("8", ("ins", "8"), 1, 1, 1),
    ("9", ("ins", "9"), 2, 1, 1), ("÷", ("ins", "/"), 3, 1, 1),
    ("4", ("ins", "4"), 0, 2, 1), ("5", ("ins", "5"), 1, 2, 1),
    ("6", ("ins", "6"), 2, 2, 1), ("×", ("ins", "*"), 3, 2, 1),
    ("1", ("ins", "1"), 0, 3, 1), ("2", ("ins", "2"), 1, 3, 1),
    ("3", ("ins", "3"), 2, 3, 1), ("−", ("ins", "-"), 3, 3, 1),
    ("0", ("ins", "0"), 0, 4, 1), (".", ("ins", "."), 1, 4, 1),
    ("^", ("ins", "^"), 2, 4, 1), ("+", ("ins", "+"), 3, 4, 1),
    ("=", ("cmd", "eval"), 0, 5, 4),
    ("Copy", ("cmd", "copy"), 0, 6, 2), ("AutoCopy", ("cmd", "auto"), 2, 6, 2),
]
CELL_W, CELL_H, HIST = 8, 2, 3
BOX_W = CELL_W * 4 + 2
BOX_H = HIST + 3 + CELL_H * 7 + 2     # history, display(2)+rule, buttons, borders


class App:
    def __init__(self, scr):
        self.scr = scr
        self.buf, self.pos = "", 0
        self.hist = []
        self.recall = None
        self.ans = 0
        self.last = None
        self.auto = False
        self.status = "Click or type. Enter = evaluate"
        self.rects = []          # (y, x, h, w, action)
        self.flash = None

    # -- actions --
    def do_copy(self):
        if self.last is None:
            self.status = "Nothing to copy yet"
            return
        how = copy_to_clipboard(self.last)
        self.status = (f"Copied {self.last} ({how})" if how
                       else "Copy failed: no clipboard tool found")

    def submit(self):
        expr = self.buf.strip()
        if not expr:
            return
        try:
            v = evaluate(expr, self.ans)
            if isinstance(v, complex) or (isinstance(v, float) and not math.isfinite(v)):
                raise ValueError("result not finite")
            self.ans, self.last = v, fmt(v)
            self.hist.append((expr, self.last))
            self.status = "OK"
            if self.auto:
                self.do_copy()
        except ZeroDivisionError:
            self.hist.append((expr, "error")); self.status = "Divide by zero"
        except Exception as ex:
            self.hist.append((expr, "error")); self.status = f"Error: {ex}"
        self.buf, self.pos, self.recall = "", 0, None

    def insert(self, text):
        if not self.buf and text in ("+", "-", "*", "/", "^", "%") and self.last is not None:
            self.buf, self.pos = "ans", 3
        self.buf = self.buf[:self.pos] + text + self.buf[self.pos:]
        self.pos += len(text)

    def run_action(self, act):
        kind, arg = act
        if kind == "ins":
            self.insert(arg)
        elif arg == "eval":
            self.submit()
        elif arg == "back":
            if self.pos:
                self.buf = self.buf[:self.pos - 1] + self.buf[self.pos:]
                self.pos -= 1
        elif arg == "clear":
            self.buf, self.pos, self.recall = "", 0, None
        elif arg == "copy":
            self.do_copy()
        elif arg == "auto":
            self.auto = not self.auto
            self.status = "Auto-copy " + ("ON" if self.auto else "OFF")
            if self.auto and self.last is not None:
                self.do_copy()

    # -- drawing --
    def draw(self):
        s = self.scr
        H, W = s.getmaxyx()
        s.erase()
        G = curses.color_pair(1)
        REV = curses.color_pair(1) | curses.A_REVERSE
        if H < BOX_H + 1 or W < BOX_W + 1:
            s.addnstr(0, 0, f"Terminal too small (need {BOX_W+1}x{BOX_H+1})", W - 1, G)
            s.refresh()
            return
        y0, x0 = (H - BOX_H - 1) // 2, (W - BOX_W) // 2
        # frame
        s.attron(G)
        s.addstr(y0, x0, "┌" + "─" * (BOX_W - 2) + "┐")
        for i in range(1, BOX_H - 1):
            s.addstr(y0 + i, x0, "│" + " " * (BOX_W - 2) + "│")
        s.addstr(y0 + BOX_H - 1, x0, "└" + "─" * (BOX_W - 2) + "┘")
        s.addstr(y0, x0 + 2, " ccalc ")
        s.attroff(G)
        inner = BOX_W - 4
        # history
        shown = self.hist[-HIST:]
        for i, (e, r) in enumerate(shown):
            y = y0 + 1 + HIST - len(shown) + i
            txt = f"{e} = {r}" if r != "error" else f"{e}  ERR"
            s.addnstr(y, x0 + 2, txt[-inner:].rjust(inner), inner, G | curses.A_DIM)
        # display: input line + result line
        yd = y0 + 1 + HIST
        shown_buf = self.buf[-(inner - 1):] if len(self.buf) >= inner else self.buf
        s.addnstr(yd, x0 + 2, shown_buf, inner, G | curses.A_BOLD)
        res = self.last if self.last is not None else "0"
        s.addnstr(yd + 1, x0 + 2, res[-inner:].rjust(inner), inner, G | curses.A_BOLD)
        s.addstr(yd + 2, x0 + 1, "─" * (BOX_W - 2), G)
        # buttons
        yb = yd + 3
        self.rects = []
        for label, act, c, r, span in LAYOUT:
            bx, by = x0 + 1 + c * CELL_W, yb + r * CELL_H
            bw = span * CELL_W - 1
            if act == ("cmd", "auto"):
                label = "Auto:" + ("ON" if self.auto else "OFF") + "  F3"
            elif act == ("cmd", "copy"):
                label = "Copy  F2"
            on = act == ("cmd", "auto") and self.auto
            attr = REV | curses.A_BOLD if (on or self.flash == act) else REV
            s.addstr(by, bx, label.center(bw), attr)
            self.rects.append((by, bx, CELL_H, span * CELL_W, act))
        s.addnstr(y0 + BOX_H, 0, self.status, W - 1, G | curses.A_DIM)
        self.cursor = (yd, min(x0 + 2 + len(shown_buf[:self.pos]) , x0 + BOX_W - 3))
        try:
            s.move(*self.cursor)
        except curses.error:
            pass
        s.refresh()

    # -- input --
    def key(self, ch):
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, st = curses.getmouse()
            except curses.error:
                return
            if st & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                for y, x, h, w, act in self.rects:
                    if y <= my < y + h and x <= mx < x + w:
                        self.run_action(act)
                        break
            return
        if ch == curses.KEY_F2 or ch == "\x19":        # Ctrl-Y
            self.run_action(("cmd", "copy"))
        elif ch == curses.KEY_F3 or ch == "\x14":      # Ctrl-T
            self.run_action(("cmd", "auto"))
        elif ch in ("\n", "\r", "=", curses.KEY_ENTER):
            self.submit()
        elif ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            self.run_action(("cmd", "back"))
        elif ch == curses.KEY_DC:
            self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]
        elif ch == curses.KEY_LEFT:
            self.pos = max(0, self.pos - 1)
        elif ch == curses.KEY_RIGHT:
            self.pos = min(len(self.buf), self.pos + 1)
        elif ch in (curses.KEY_HOME, "\x01"):
            self.pos = 0
        elif ch in (curses.KEY_END, "\x05"):
            self.pos = len(self.buf)
        elif ch == curses.KEY_UP and self.hist:
            self.recall = len(self.hist) - 1 if self.recall is None else max(0, self.recall - 1)
            self.buf = self.hist[self.recall][0]; self.pos = len(self.buf)
        elif ch == curses.KEY_DOWN and self.recall is not None:
            self.recall += 1
            self.buf = "" if self.recall >= len(self.hist) else self.hist[self.recall][0]
            if self.recall >= len(self.hist):
                self.recall = None
            self.pos = len(self.buf)
        elif ch in ("\x1b", "\x15"):
            self.run_action(("cmd", "clear"))
        elif isinstance(ch, str) and ch.isprintable():
            self.insert(ch)

    def run(self):
        while True:
            self.draw()
            try:
                ch = self.scr.get_wch()
            except KeyboardInterrupt:
                return
            except curses.error:
                continue
            self.key(ch)


def main(scr):
    curses.curs_set(1)
    curses.start_color()
    curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
    scr.bkgd(" ", curses.color_pair(1))
    scr.keypad(True)
    curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)
    curses.set_escdelay(25)
    App(scr).run()


def cli():
    if len(sys.argv) > 1:       # one-shot: ccalc 2+2
        try:
            print(fmt(evaluate(" ".join(sys.argv[1:]), 0)))
        except Exception as ex:
            sys.exit(f"ccalc: {ex}")
    else:
        curses.wrapper(main)


if __name__ == "__main__":
    cli()
