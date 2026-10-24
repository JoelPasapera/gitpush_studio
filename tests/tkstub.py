# -*- coding: utf-8 -*-
"""Sustitutos mínimos de tkinter y customtkinter para probar el cableado
de la interfaz sin pantalla ni dependencias.

No dibujan nada: guardan texto/valores, aceptan cualquier argumento de
estilo y simulan `after` con una cola que los tests vacían a mano
(`Scheduler.pump`). Los métodos desconocidos NO se toleran (salvo
`winfo_*`), para que una errata en el código real siga fallando.
"""

import sys
import time
import types


class Scheduler:
    pending = []
    counter = 0

    @classmethod
    def after(cls, ms, fn=None, *args):
        cls.counter += 1
        cid = f"after#{cls.counter}"
        if fn is not None:
            cls.pending.append((cid, ms, fn, args))
        return cid

    @classmethod
    def cancel(cls, cid):
        cls.pending = [p for p in cls.pending if p[0] != cid]

    @classmethod
    def run_due(cls, max_ms=1000):
        due = [p for p in cls.pending if p[1] <= max_ms]
        cls.pending = [p for p in cls.pending if p[1] > max_ms]
        for _, _, fn, args in due:
            fn(*args)
        return len(due)

    @classmethod
    def pump(cls, until, timeout=30.0, step=None):
        """Ejecuta callbacks pendientes hasta que `until()` sea cierto."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            cls.run_due()
            if step:
                step()
            if until():
                return True
            time.sleep(0.02)
        raise TimeoutError("la condición no se cumplió a tiempo")


class Clipboard:
    """Portapapeles falso, para comprobar qué se copió."""
    text = ""


class Event:
    def __init__(self, keysym="", state=0, **kw):
        self.keysym = keysym
        self.state = state
        for k, v in kw.items():
            setattr(self, k, v)


class Var:
    def __init__(self, value=None, **kw):
        self._v = value

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


class Widget:
    def __init__(self, master=None, **kw):
        self.master = master
        self._kw = dict(kw)
        self._yview = (0.0, 1.0)
        self._children = []
        self._exists = True
        self._value = kw.get("value", 0)
        if master is not None and hasattr(master, "_children"):
            master._children.append(self)

    # geometría y configuración -------------------------------------
    def grid(self, **kw):
        return self

    def pack(self, **kw):
        return self

    def grid_remove(self):
        pass

    def grid_propagate(self, *a):
        pass

    def grid_columnconfigure(self, *a, **kw):
        pass

    def grid_rowconfigure(self, *a, **kw):
        pass

    def configure(self, **kw):
        self._kw.update(kw)

    config = configure

    def cget(self, key):
        return self._kw.get(key)

    @property
    def text(self):
        return self._kw.get("text", "")

    # ciclo de vida ---------------------------------------------------
    def destroy(self):
        self._exists = False
        for c in list(self._children):
            c.destroy()
        if self.master is not None and hasattr(self.master, "_children"):
            if self in self.master._children:
                self.master._children.remove(self)

    def winfo_exists(self):
        return self._exists

    def winfo_children(self):
        return list(self._children)

    def __getattr__(self, name):
        if name.startswith("winfo_"):
            return lambda *a, **k: 800
        raise AttributeError(name)

    # planificación ---------------------------------------------------
    def after(self, ms, fn=None, *args):
        return Scheduler.after(ms, fn, *args)

    def after_cancel(self, cid):
        Scheduler.cancel(cid)

    # ventanas --------------------------------------------------------
    def title(self, *a):
        pass

    def geometry(self, *a):
        pass

    def minsize(self, *a):
        pass

    def resizable(self, *a):
        pass

    def transient(self, *a):
        pass

    def grab_set(self):
        pass

    def protocol(self, *a):
        pass

    def lift(self):
        pass

    def focus(self):
        pass

    def bell(self):
        pass

    def bind(self, sequence=None, command=None, add=None):
        self._bindings = getattr(self, "_bindings", {})
        self._bindings[sequence] = command

    def event(self, sequence, **kw):
        """Dispara un binding como haría Tk; devuelve lo que retorne."""
        fn = getattr(self, "_bindings", {}).get(sequence)
        return fn(Event(**kw)) if fn else None

    def focus_set(self):
        pass

    def yview(self, *a):
        return self._yview

    def clipboard_clear(self):
        Clipboard.text = ""

    def clipboard_append(self, text):
        Clipboard.text += text

    def withdraw(self):
        pass

    def mainloop(self):
        pass

    # valor (progress bar, option menu, segmented) ---------------------
    def set(self, v):
        self._value = v

    def get(self):
        return self._value


class Entry(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._text = ""

    def insert(self, index, s):
        self._text = (self._text + s) if index in ("end", "insert") \
            else (s + self._text)

    def delete(self, a, b=None):
        self._text = ""

    def get(self):
        return self._text


class Textbox(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._text = ""
        self._sel = None

    def insert(self, index, s, *tags):
        self._text = (self._text + s) if index == "end" else (s + self._text)

    def delete(self, a, b=None):
        self._text = ""
        self._sel = None          # Tk también pierde la selección

    def get(self, a, b=None):
        if a == "sel.first":
            if self._sel is None:
                raise RuntimeError("sin selección")
            return self._sel
        return self._text

    def tag_add(self, tag, a, b):
        if tag == "sel":
            self._sel = self._text.rstrip("\n")

    def tag_ranges(self, tag):
        return (1, 2) if self._sel else ()

    def see(self, *a):
        pass

    def tag_config(self, *a, **kw):
        pass


class InputDialog(Widget):
    RESPONSE = ""

    def get_input(self):
        return self.RESPONSE


class Font:
    def __init__(self, **kw):
        self.kw = kw


class Messagebox:
    ANSWER = True
    calls = []

    @classmethod
    def _rec(cls, kind, *a):
        cls.calls.append((kind, a))

    @classmethod
    def showerror(cls, *a, **k):
        cls._rec("error", *a)

    @classmethod
    def showinfo(cls, *a, **k):
        cls._rec("info", *a)

    @classmethod
    def showwarning(cls, *a, **k):
        cls._rec("warning", *a)

    @classmethod
    def askyesno(cls, *a, **k):
        cls._rec("askyesno", *a)
        return cls.ANSWER


class Filedialog:
    FOLDER = ""
    FILE = ""

    @classmethod
    def askdirectory(cls, **k):
        return cls.FOLDER

    @classmethod
    def asksaveasfilename(cls, **k):
        return cls.FILE


def install():
    """Registra los módulos falsos (idempotente)."""
    if "customtkinter" in sys.modules and getattr(
            sys.modules["customtkinter"], "_stub", False):
        return
    tk = types.ModuleType("tkinter")
    tk.TclError = type("TclError", (Exception,), {})
    tk.Tk = Widget
    tk.BooleanVar = tk.StringVar = tk.IntVar = Var
    fd = types.ModuleType("tkinter.filedialog")
    fd.askdirectory = Filedialog.askdirectory
    fd.asksaveasfilename = Filedialog.asksaveasfilename
    mb = types.ModuleType("tkinter.messagebox")
    for name in ("showerror", "showinfo", "showwarning", "askyesno"):
        setattr(mb, name, getattr(Messagebox, name))
    tk.filedialog, tk.messagebox = fd, mb
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.filedialog"] = fd
    sys.modules["tkinter.messagebox"] = mb

    ctk = types.ModuleType("customtkinter")
    ctk._stub = True
    ctk.__version__ = "stub"
    ctk.CTk = type("CTk", (Widget,), {})
    ctk.CTkToplevel = type("CTkToplevel", (Widget,), {})
    for name in ("CTkFrame", "CTkLabel", "CTkButton", "CTkCheckBox",
                 "CTkSwitch", "CTkProgressBar", "CTkOptionMenu",
                 "CTkSegmentedButton", "CTkScrollableFrame"):
        setattr(ctk, name, type(name, (Widget,), {}))
    ctk.CTkEntry = Entry
    ctk.CTkTextbox = Textbox
    ctk.CTkInputDialog = InputDialog
    ctk.CTkFont = Font
    ctk.BooleanVar = ctk.StringVar = ctk.IntVar = Var
    ctk.set_appearance_mode = lambda *a, **k: None
    sys.modules["customtkinter"] = ctk
