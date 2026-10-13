"""Tk presentation helpers. Device communication stays in bronkhorst_gui."""

import tkinter as tk
from tkinter import font as tkfont, ttk
from experiment_cycle import OPERATION_LABELS


ROLE_LABELS = {"Unassigned": "Не назначена", "H2": "Водород (H₂)", "Alkene": "Алкен", "Other": "Другой газ"}
MODE_LABELS = {"Read flow": "Чтение потока", "Set flow": "Управление потоком"}
BG, INK, MUTED, ACCENT = "#f3f5f7", "#243344", "#596b7d", "#206d83"


def configure_style(root):
    # Use the platform's compact controls, respecting Windows DPI scaling.
    style = ttk.Style(root)
    style.theme_use("vista" if "vista" in style.theme_names() else "clam")
    global BG
    BG = "#ffffff"
    root.configure(background=BG)
    style.configure(".", font=("Segoe UI", 9), background=BG)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG)
    style.configure("TLabelframe", background=BG, borderwidth=0, relief="flat")
    style.configure("Muted.TLabel", foreground="#606060")
    style.configure("Status.TLabel", foreground="#333333")
    style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"), background=BG)
    style.configure("TButton", padding=(6, 2))
    style.configure("Primary.TButton", font=("Segoe UI", 9))
    style.configure("Stop.TButton", font=("Segoe UI", 9))
    style.configure("Stop.TButton", foreground="#b42318")
    style.configure("TEntry", padding=2)
    style.configure("TCombobox", padding=2)
    style.configure("TNotebook.Tab", padding=(9, 3))
    row_font = tkfont.Font(root, font=("Segoe UI", 9))
    style.configure("Treeview", rowheight=row_font.metrics("linespace") + 6)
    style.configure("Treeview.Heading", font=("Segoe UI", 9), padding=(4, 3))


class ChoiceBox(ttk.Combobox):
    """Show translated choices while preserving stored/protocol values."""

    def __init__(self, master, variable, labels, **kwargs):
        self.value = variable
        self.labels = labels
        self.display = tk.StringVar(master, value=labels.get(variable.get(), variable.get()))
        super().__init__(master, textvariable=self.display, values=list(labels.values()), **kwargs)
        self._trace = variable.trace_add("write", self._refresh)
        self.bind("<<ComboboxSelected>>", self._selected)
        self.bind("<Destroy>", self._destroy_trace)

    def _refresh(self, *_args):
        self.display.set(self.labels.get(self.value.get(), self.value.get()))

    def _selected(self, _event):
        self.value.set(next(key for key, label in self.labels.items() if label == self.display.get()))

    def _destroy_trace(self, event):
        if event.widget is self:
            self.value.trace_remove("write", self._trace)


class ScrollPanel(ttk.Frame):
    """Scrollable form with wheel bindings scoped to its own descendants."""

    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, background=BG, highlightthickness=0, width=1, height=1)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(self.canvas, padding=(0, 2, 6, 6))
        self.window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.window, width=event.width))
        self.content.bind("<Configure>", self._layout)
        self._wheel_tag = f"scroll:{self}"
        self.bind_class(self._wheel_tag, "<MouseWheel>", self._wheel)
        self.bind_class(self._wheel_tag, "<Button-4>", self._wheel)
        self.bind_class(self._wheel_tag, "<Button-5>", self._wheel)
        self.bind("<Destroy>", self._cleanup)

    def _layout(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._tag(self.content)
        self._tag(self.canvas, recurse=False)

    def _tag(self, widget, recurse=True):
        # Tables retain their own wheel scrolling; the surrounding form scrolls
        # when the pointer is over labels, fields, or whitespace.
        if not isinstance(widget, (ttk.Treeview, ttk.Combobox, tk.Text, ttk.Scrollbar)):
            tags = widget.bindtags()
            if self._wheel_tag not in tags:
                widget.bindtags((tags[0], self._wheel_tag, *tags[1:]))
        if recurse:
            for child in widget.winfo_children():
                self._tag(child)

    def _wheel(self, event):
        if self.canvas.yview() == (0.0, 1.0):
            return
        steps = -1 if getattr(event, "num", 0) == 4 else 1 if getattr(event, "num", 0) == 5 else (-1 if event.delta > 0 else 1)
        self.canvas.yview_scroll(steps * 3, "units")
        return "break"

    def _cleanup(self, event):
        if event.widget is self:
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.unbind_class(self._wheel_tag, sequence)


def note(parent, text=None, variable=None, style="Muted.TLabel"):
    label = ttk.Label(parent, text=text, textvariable=variable, style=style, wraplength=600)
    parent.bind("<Configure>", lambda event: label.configure(wraplength=max(240, event.width - 32)), add="+")
    return label


def section(parent, title):
    frame = ttk.LabelFrame(parent, text=title, padding=(8, 4))
    frame.pack(fill="x", pady=(0, 6))
    return frame


def table(parent, columns, height=5):
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    tree = ttk.Treeview(frame, columns=[key for key, _label, _width in columns], show="headings", height=height, selectmode="browse")
    for key, label, width in columns:
        tree.heading(key, text=label, anchor="w")
        heading_font = tkfont.Font(tree, font=("Segoe UI", 9))
        minimum = max(80, heading_font.measure(label) + 24)
        tree.column(key, width=max(width, minimum), minwidth=minimum, anchor="w", stretch=True)
    tree.grid(row=0, column=0, sticky="nsew")
    vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    horizontal.grid(row=1, column=0, sticky="ew")
    tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    return tree


class CycleDiagram(tk.Canvas):
    """A schematic, not a time-scaled plot: acquisition duration is unknown."""

    def __init__(self, master, variables, select_delay):
        super().__init__(master, height=280, background="white", highlightthickness=0)
        self.variables, self.select_delay = variables, select_delay
        self.valves_enabled = False
        self.active_stage = None
        self.bind("<Configure>", lambda _event: self.redraw())
        for variable in variables.values():
            variable.trace_add("write", lambda *_args: self.redraw())

    def redraw(self):
        self.delete("all")
        self.configure(scrollregion=(0, 0, 940, 280))
        blue, green, grey = "#206d83", "#49795b", "#687887"
        self.create_text(22, 18, anchor="w", text="Одна точка · два спектра · длительность записи определяется ответным TTL", fill=grey)
        self.create_text(40, 60, text="Новый\nрасход", fill=blue)
        self.create_line(40, 82, 40, 104, fill=blue, arrow="last")
        coords = [40, 100, 210, 100, 210, 74, 220, 74, 220, 100,
                  300, 100, 300, 74, 310, 74, 310, 100,
                  620, 100, 620, 74, 630, 74, 630, 100,
                  710, 100, 710, 74, 720, 74, 720, 100, 900, 100]
        self.create_line(*coords, fill=blue, width=2)
        for x, text in ((210, "TTL → ЯМР"), (300, "TTL ← ЯМР"), (620, "TTL → ЯМР"), (710, "TTL ← ЯМР")):
            self.create_text(x, 57, text=text, fill=blue, font=("Segoe UI", 8))
        for key, x1, x2 in (("t1", 45, 200), ("t2", 315, 445), ("t3", 455, 610), ("t4", 725, 875)):
            active = self.active_stage == key
            tag = f"delay-{key}"
            self.create_line(x1, 32, x2, 32, arrow="both", fill=blue, tags=tag)
            self.create_text((x1 + x2) / 2, 46, text=f"{key} = {self.variables[key].get()} с",
                             fill="#b45309" if active else blue, font=("Segoe UI", 10, "bold"), tags=tag)
            self.tag_bind(tag, "<Button-1>", lambda _event, name=key: self.select_delay(name))
            self.tag_bind(tag, "<Enter>", lambda _event: self.configure(cursor="hand2"))
            self.tag_bind(tag, "<Leave>", lambda _event: self.configure(cursor=""))
        for spectrum, x1, x2 in ((1, 210, 310), (2, 620, 720)):
            active = self.active_stage == f"nmr{spectrum}"
            self.create_rectangle(x1, 120, x2, 158, fill="#fff1d6" if active else "#e8f1f5", outline=blue)
            self.create_text((x1 + x2) / 2, 139, text=f"Спектр №{spectrum}", fill=blue)
        self.create_text(22, 184, anchor="w", text="Питание двух клапанов (реле D4/D5)" if self.valves_enabled else "Реле: не используются в этом режиме", fill=green if self.valves_enabled else grey)
        if self.valves_enabled:
            self.create_line(40, 240, 450, 240, 450, 208, 880, 208, 880, 240, 900, 240, fill=green, width=2)
            self.create_text(450, 253, text="Реле ON", fill=green)
            self.create_text(880, 253, text="Реле OFF", fill=green)
            self.create_text(250, 224, text="24 В снято", fill=grey)
            self.create_text(665, 224, text="24 В подано", fill=green)
        else:
            self.create_line(40, 240, 900, 240, fill=grey, dash=(5, 4))
        self.create_text(900, 120, anchor="e", text="После t4 →\nследующая точка", fill=grey)


def build_cycle_editor(app):
    cycle = section(app.design_content, "Цикл одной экспериментальной точки")
    note(cycle, "Расход удерживается на протяжении всего цикла. Нажмите t1–t4 на схеме, чтобы перейти к соответствующему полю. Схема не в масштабе времени.").pack(fill="x", pady=(0, 6))
    diagram_frame = ttk.Frame(cycle)
    diagram_frame.pack(fill="x")
    app.cycle_diagram = CycleDiagram(diagram_frame, app.cycle_vars, app._focus_cycle_delay)
    app.cycle_diagram.pack(fill="x")
    scrollbar = ttk.Scrollbar(diagram_frame, orient="horizontal", command=app.cycle_diagram.xview)
    scrollbar.pack(fill="x")
    app.cycle_diagram.configure(xscrollcommand=scrollbar.set)
    fields = ttk.Frame(cycle)
    fields.pack(fill="x", pady=(8, 0))
    app.cycle_entries = {}
    descriptions = {"t1": "После изменения расхода", "t2": "После спектра №1", "t3": "До запуска спектра №2", "t4": "После спектра №2"}
    for i, (key, variable) in enumerate(app.cycle_vars.items()):
        cell = ttk.Frame(fields, padding=(0, 0, 12, 6))
        cell.grid(row=i // 2, column=i % 2, sticky="ew")
        fields.columnconfigure(i % 2, weight=1, uniform="cycle")
        ttk.Label(cell, text=f"{key}, с · {descriptions[key]}").pack(anchor="w")
        entry = ttk.Entry(cell, textvariable=variable, width=14)
        entry.pack(fill="x", pady=(4, 0))
        app.cycle_entries[key] = entry
        app.plan_input_widgets.append(entry)
    note(cycle, "После t2: оба реле ON (клапан 1 закрыт, клапан 2 открыт). После t4: оба OFF (исходные положения). Без клапанов переключения пропускаются, задержки сохраняются.").pack(fill="x")
    note(cycle, "Arduino подтверждает уровни выходов D4/D5, но положение клапанов отдельными датчиками не измеряется.").pack(fill="x", pady=(6, 0))
    actions = ttk.Frame(app.design_content)
    actions.pack(fill="x", pady=(4, 8))
    app.save_design_button = ttk.Button(actions, text="Сохранить дизайн", command=app._save_experiment_design)
    app.save_design_button.pack(side="left")
    ttk.Button(actions, text="Закрыть", command=app.design_window.withdraw).pack(side="right")
    note(app.design_content, variable=app.design_status_var).pack(fill="x")


def build_interface(app):
    """Build the four tabs and a fixed experiment control bar."""
    body = ttk.Frame(app, padding=8)
    body.pack(fill="both", expand=True)
    app.device_count_var = tk.StringVar(app, value="Нет подключённых MFC")
    footer = ttk.Frame(body)
    footer.pack(side="bottom", fill="x", pady=(4, 0))
    ttk.Label(footer, textvariable=app.device_count_var, style="Muted.TLabel").pack(side="right", padx=(12, 0))
    warning = note(footer, variable=app.port_warning_var)
    warning.configure(foreground="#a83d3d")
    status_label = note(footer, variable=app.status_var)
    status_label.pack(anchor="w", fill="x")
    def show_warning(*_args):
        if app.port_warning_var.get():
            warning.pack(anchor="w", fill="x", before=status_label)
        else:
            warning.pack_forget()
    app.port_warning_var.trace_add("write", show_warning)
    app.notebook = ttk.Notebook(body)
    app.notebook.pack(fill="both", expand=True)
    panels = []
    for title in ("Подключение", "Параметры газа", "Эксперимент", "Консоль"):
        panel = ttk.Frame(app.notebook, padding=(6, 6, 0, 0))
        app.notebook.add(panel, text=title)
        panels.append(panel)
    connection_tab, calibration_tab, experiment_tab, console_tab = panels
    app.form_panels = []
    for parent in (connection_tab, calibration_tab, experiment_tab):
        scroll = ScrollPanel(parent)
        scroll.pack(fill="both", expand=True)
        app.form_panels.append(scroll)
    connection, calibration, experiment = [panel.content for panel in app.form_panels]

    connect = section(connection, "Подключить расходомер")
    ttk.Label(connect, text="COM-порт").pack(side="left", padx=(0, 8))
    app.available_port_box = ttk.Combobox(connect, textvariable=app.available_port_var, state="readonly", width=12)
    app.available_port_box.pack(side="left", padx=(0, 12))
    ttk.Button(connect, text="Обновить", command=app._update_available_ports).pack(side="left")
    ttk.Button(connect, text="Подключить", style="Primary.TButton", command=app._connect_selected_port).pack(side="left", padx=8)
    ttk.Button(connect, text="Отключить", command=app._disconnect_selected_port).pack(side="left")
    devices = section(connection, "Подключённые приборы")
    app.tree = table(devices, [("serial", "Серийный номер", 155), ("role", "Роль", 130), ("mode", "Режим", 185), ("port", "Порт", 80), ("address", "Протокол", 90), ("model", "Модель", 225), ("capacity", "Диапазон", 155), ("measured", "Текущий поток", 155)])
    app.tree.configure(displaycolumns=("serial", "port", "measured", "capacity", "role", "mode"))
    app.tree.heading("serial", text="Прибор")
    for key, width in (("serial", 140), ("port", 70), ("measured", 140), ("capacity", 130), ("role", 120), ("mode", 165)):
        app.tree.column(key, width=width, minwidth=width)
    app.tree.bind("<<TreeviewSelect>>", app._select_connection_device)
    app.devices_empty_label = note(devices, "Приборов пока нет. Выберите COM-порт и нажмите «Подключить».")
    app.devices_empty_label.pack(anchor="w", pady=(4, 0))
    selected = section(connection, "Настройки выбранного прибора")
    ttk.Label(selected, text="Прибор").grid(row=0, column=0, sticky="w", pady=5)
    app.role_device_box = ttk.Combobox(selected, textvariable=app.role_device_var, state="disabled", width=24)
    app.role_device_box.grid(row=0, column=1, sticky="w", padx=12, pady=5)
    app.role_device_box.bind("<<ComboboxSelected>>", app._load_role)
    app.device_detail_var = tk.StringVar(app)
    note(selected, variable=app.device_detail_var).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 0))
    ttk.Label(selected, text="Роль в смеси").grid(row=1, column=0, sticky="w", pady=5)
    ChoiceBox(selected, app.role_var, ROLE_LABELS, state="readonly", width=24).grid(row=1, column=1, padx=12, pady=5)
    ttk.Button(selected, text="Сохранить роль", command=app._save_role).grid(row=1, column=2, sticky="w")
    ttk.Label(selected, text="Режим MFC").grid(row=2, column=0, sticky="w", pady=5)
    app.device_mode_box = ChoiceBox(selected, app.device_mode_var, MODE_LABELS, state="disabled", width=24)
    app.device_mode_box.grid(row=2, column=1, padx=12, pady=5)
    ttk.Button(selected, text="Сохранить режим", command=app._save_device_mode).grid(row=2, column=2, sticky="w")
    note(connection, "Один MFC получает весь поток. Для смеси назначьте роли «Водород» и «Алкен» двум приборам; «Другой газ» в смесь не входит.").pack(fill="x", pady=(0, 10))
    note(connection, "Подключение только считывает параметры. Команды подачи газа отправляются на вкладке «Эксперимент».").pack(fill="x")

    overview = section(calibration, "Газовые профили приборов")
    app.calibration_tree = table(overview, [("serial", "Прибор", 145), ("port", "Порт", 80), ("gas", "Газ", 95), ("fluidset", "Профиль", 80), ("inlet", "Вход, бар(a)", 120), ("outlet", "Выход, бар(a)", 120), ("temperature", "T, °C", 90), ("reference", "Опорное, бар(a)", 140), ("capacity", "Диапазон", 155)], height=4)
    app.calibration_tree.bind("<<TreeviewSelect>>", app._select_calibration_device)
    note(overview, "Выберите прибор в таблице, чтобы изменить его профиль.").pack(anchor="w", pady=(4, 0))
    conditions = section(calibration, "Параметры выбранного профиля")
    note(conditions, variable=app.fluid_conditions_var, style="Status.TLabel").pack(fill="x", pady=(0, 12))
    fields = ttk.Frame(conditions)
    fields.pack(fill="x")
    for i, (label, variable) in enumerate((("Профиль газа (0–7)", app.fluidset_var), ("Ожидаемый газ", app.expected_gas_var), ("Давление на входе, бар(a)", app.inlet_pressure_var), ("Давление на выходе, бар(a)", app.outlet_pressure_var), ("Температура газа, °C", app.fluid_temperature_var), ("Опорное давление, бар(a)", app.reference_pressure_var))):
        cell = ttk.Frame(fields, padding=(0, 0, 12, 6))
        cell.grid(row=i // 2, column=i % 2, sticky="ew")
        fields.columnconfigure(i % 2, weight=1, uniform="conditions")
        ttk.Label(cell, text=label, style="Muted.TLabel").pack(anchor="w", pady=(0, 5))
        if i == 0:
            ttk.Spinbox(cell, from_=0, to=7, textvariable=variable, width=12).pack(fill="x")
        else:
            ttk.Entry(cell, textvariable=variable, width=12).pack(fill="x")
    ttk.Checkbutton(conditions, text="Разрешить запись параметров в прибор", variable=app.control_enabled, command=app._toggle_control).pack(anchor="w", pady=(0, 8))
    buttons = ttk.Frame(conditions)
    buttons.pack(fill="x")
    ttk.Button(buttons, text="Активировать профиль", command=app._activate_fluidset).pack(side="left")
    ttk.Button(buttons, text="Сохранить условия газа", command=app._apply_fluid_conditions).pack(side="left", padx=10)
    note(calibration, "Это сохранённые условия газового профиля, а не текущие показания датчиков давления и температуры. Выбирайте профиль для фактически подключённого газа.").pack(fill="x")

    mode = ttk.Frame(experiment, padding=(4, 4, 4, 10))
    mode.pack(fill="x")
    app.mode_buttons = []
    row = ttk.Frame(mode)
    row.pack(fill="x")
    for value, label in OPERATION_LABELS.items():
        button = ttk.Radiobutton(row, text=label, variable=app.operation_mode, value=value, command=app._operation_mode_changed)
        button.pack(side="left", padx=(0, 22))
        app.mode_buttons.append(button)
    ttk.Button(mode, text="Дизайн эксперимента ↗", command=app._open_design_window).pack(anchor="w", pady=(8, 0))
    note(mode, variable=app.operation_mode_hint).pack(fill="x", pady=(6, 0))
    app.experiment_columns = ttk.Frame(experiment)
    app.experiment_columns.pack(fill="both", expand=True)
    app.experiment_columns.columnconfigure(0, weight=2, minsize=300)
    app.experiment_columns.columnconfigure(1, weight=3, minsize=360)
    left = ttk.Frame(app.experiment_columns, padding=(0, 0, 10, 0))
    right = ttk.Frame(app.experiment_columns)
    left.grid(row=0, column=0, sticky="nsew")
    right.grid(row=0, column=1, sticky="nsew")
    app.arduino_frame = ttk.LabelFrame(left, text="Синхронизация с ЯМР", padding=(8, 4))
    arduino_fields = ttk.Frame(app.arduino_frame)
    arduino_fields.pack(fill="x")
    for i, (label, var) in enumerate((("COM-порт Arduino", app.arduino_port_var), ("Скорость, бод", app.arduino_baudrate_var), ("Ожидание ответа, с", app.arduino_timeout_var))):
        ttk.Label(arduino_fields, text=label, style="Muted.TLabel").grid(row=i, column=0, sticky="w", padx=(0, 10), pady=1)
        if i == 0:
            app.arduino_port_box = ttk.Combobox(arduino_fields, textvariable=var, width=16, state="readonly")
            widget = app.arduino_port_box
        else:
            widget = ttk.Entry(arduino_fields, textvariable=var, width=16)
        widget.grid(row=i, column=1, sticky="ew", pady=1)
    arduino_fields.columnconfigure(1, weight=1)
    arduino_actions = ttk.Frame(app.arduino_frame)
    arduino_actions.pack(fill="x", pady=(6, 0))
    app.arduino_connect_button = ttk.Button(arduino_actions, text="Подключить Arduino", command=app._connect_arduino)
    app.arduino_connect_button.pack(side="left")
    ttk.Button(arduino_actions, text="Отключить", command=app._disconnect_arduino).pack(side="left", padx=8)
    note(app.arduino_frame, variable=app.arduino_status_var).pack(fill="x", pady=(4, 0))
    note(app.arduino_frame, "D8 · TTL 5 мс; D2 · ответ ЯМР\nD4 → IN1; D5 → IN2; LOW = реле ON").pack(fill="x", pady=(6, 0))
    app.design_summary_frame = section(left, "Дизайн эксперимента")
    ttk.Button(app.design_summary_frame, text="Открыть дизайн эксперимента ↗", command=app._open_design_window).pack(anchor="w")
    note(app.design_summary_frame, variable=app.design_summary_var).pack(fill="x", pady=(6, 0))
    app.valve_status_label = note(app.design_summary_frame, variable=app.valve_status_var)
    app.valve_status_label.pack(fill="x", pady=(4, 0))
    app.design_window = tk.Toplevel(app)
    app.design_window.withdraw()
    app.design_window.title("Дизайн эксперимента · цикл и потоки")
    app.design_window.geometry(f"{min(1060, app.winfo_screenwidth() - 60)}x{min(780, app.winfo_screenheight() - 100)}")
    app.design_window.minsize(680, 500)
    app.design_window.protocol("WM_DELETE_WINDOW", app.design_window.withdraw)
    design_scroll = ScrollPanel(app.design_window)
    design_scroll.pack(fill="both", expand=True, padx=10, pady=10)
    app.design_content = design_scroll.content
    app.plan_frame = section(app.design_content, "План подачи газа")
    app.plan_field_labels, app.plan_field_entries, app.plan_input_widgets = {}, {}, []
    for i, (label, key) in enumerate((("Начальный поток", "start"), ("Конечный поток", "stop"), ("Шаг потока", "step"), ("H₂ : алкен", "ratio"))):
        label_widget = ttk.Label(app.plan_frame, text=label, style="Muted.TLabel")
        label_widget.grid(row=i, column=0, sticky="w", padx=(0, 12), pady=1)
        entry = ttk.Entry(app.plan_frame, textvariable=app.plan_vars[key], width=10)
        entry.grid(row=i, column=1, sticky="ew", pady=1)
        app.plan_field_labels[key], app.plan_field_entries[key] = label_widget, entry
        app.plan_input_widgets.append(entry)
    app.plan_unit_var = tk.StringVar(app, value="Единицы потока появятся после подключения MFC.")
    app.plan_frame.columnconfigure(1, weight=1)
    note(app.plan_frame, variable=app.plan_unit_var).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(4, 0))
    app.validate_plan_button = ttk.Button(app.plan_frame, text="Проверить план", command=app._validate_plan)
    app.validate_plan_button.grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
    note(app.plan_frame, variable=app.plan_var).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 0))
    build_cycle_editor(app)
    readings = section(right, "Текущие показания")
    app.live_tree = table(readings, [("device", "Прибор / порт", 155), ("gas", "Газ", 65), ("set", "Уставка", 110), ("flow", "Измерено", 110)], height=2)
    app.live_timestamp_var = tk.StringVar(app, value="Нет данных от приборов")
    note(readings, variable=app.live_timestamp_var).pack(anchor="w", pady=(4, 0))
    app.run_status_frame = section(right, "Ход эксперимента")
    note(app.run_status_frame, variable=app.progress_var, style="Status.TLabel").pack(fill="x")
    app.run_progress = ttk.Progressbar(app.run_status_frame, maximum=100)
    app.run_progress.pack(fill="x", pady=8)
    app.completed_points_var = tk.StringVar(app, value="План не запущен")
    note(app.run_status_frame, variable=app.completed_points_var).pack(fill="x")
    app.plan_view_frame = section(right, "Точки эксперимента")
    ttk.Button(app.plan_view_frame, text="Открыть график ↗", command=app._open_graph_window).pack(anchor="e", pady=(0, 4))
    app.schedule_tree = table(app.plan_view_frame, [("point", "№", 45), ("total", "Поток", 80), ("h2", "H₂", 80), ("alkene", "Алкен", 80), ("dwell", "t1, с", 80), ("status", "Состояние", 145)], height=4)
    app.manual_frame = ttk.LabelFrame(experiment, text="Потоки подключённых приборов", padding=8)
    app.manual_cards_frame = ttk.Frame(app.manual_frame)
    app.manual_cards_frame.pack(fill="both", expand=True)
    app.manual_empty_label = note(app.manual_cards_frame, "Подключите расходомер на вкладке «Подключение».")
    app.manual_empty_label.pack(anchor="w")

    recent = section(experiment, "Последние события")
    app.recent_events_frame = recent
    ttk.Button(recent, text="Вся консоль", command=lambda: app.notebook.select(console_tab)).pack(side="right", anchor="n", padx=(6, 0))
    app.recent_log = tk.Text(recent, height=3, wrap="none", state="disabled", font=("Consolas", 9), background="white", relief="flat", padx=6, pady=4)
    app.recent_log.pack(fill="x")

    # Pack this before the expanding viewport so it stays reachable on short screens.
    app.experiment_footer = ttk.Frame(experiment_tab, padding=(6, 6, 8, 0))
    app.experiment_footer.pack(side="bottom", fill="x", before=app.form_panels[2])
    app.auto_controls = ttk.Frame(app.experiment_footer)
    app.auto_controls.pack(fill="x")
    app.enable_writing_check = ttk.Checkbutton(app.auto_controls, text="Разрешить подачу газа", variable=app.control_enabled, command=app._toggle_control)
    app.enable_writing_check.pack(side="left")
    app.start_experiment_button = ttk.Button(app.auto_controls, text="Запустить", style="Primary.TButton", command=app._start_experiment)
    app.start_experiment_button.pack(side="left", padx=12)
    app.stop_experiment_button = ttk.Button(app.auto_controls, text="Стоп · поток в ноль", style="Stop.TButton", command=app._stop_experiment)
    app.stop_experiment_button.pack(side="left")
    app.progress_label = note(app.experiment_footer, variable=app.progress_var, style="Status.TLabel")
    app.progress_label.pack(fill="x", pady=(4, 0))
    note(app.experiment_footer, "Программный стоп не заменяет аппаратный запорный клапан. Проверьте герметичность линии.").pack(fill="x", pady=(4, 0))

    log_actions = ttk.Frame(console_tab)
    log_actions.pack(fill="x", pady=(0, 10))
    ttk.Label(log_actions, text="Подключения, команды и ответы приборов", style="Muted.TLabel").pack(side="left")
    ttk.Button(log_actions, text="Копировать", command=app._copy_console).pack(side="right")
    ttk.Button(log_actions, text="Очистить", command=app._clear_console).pack(side="right", padx=8)
    log_frame = ttk.Frame(console_tab)
    log_frame.pack(fill="both", expand=True)
    app.connection_log = tk.Text(log_frame, wrap="word", state="disabled", background="#192530", foreground="#dde6ed", font=("Consolas", 9), borderwidth=0, padx=8, pady=6, height=8)
    bar = ttk.Scrollbar(log_frame, orient="vertical", command=app.connection_log.yview)
    bar.pack(side="right", fill="y")
    app.connection_log.configure(yscrollcommand=bar.set)
    app.connection_log.pack(fill="both", expand=True)
    app.connection_log.tag_configure("error", foreground="#ffb4ab")
    app.connection_log.tag_configure("ttl", foreground="#8de0d0")
    app._update_operation_mode_controls()
